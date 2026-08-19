<script setup lang="ts">
// Dashboard: stat tiles + a sync→detect→group→review→apply strip + an audit
// tail, all from /api/stats + /api/audit (the old window.SYN_* server embeds are
// gone — we fetch on mount instead). Stats are pulled on mount and then only
// while (or just after) a job runs; the "is a job running" signal comes from the
// shared jobs store rather than a second /api/jobs timer of our own. The empty-DB
// CTA replaces the tiles when nothing is synced yet.
import { ref, computed, onMounted, watch } from 'vue'
import { getJSON } from '../api/client'
import { useJobs } from '../stores/jobs'
import type { Stats, AuditEntry } from '../api/types'

const stats = ref<Stats | null>(null)
const audit = ref<AuditEntry[]>([])
const loaded = ref(false)
const { state: jobs } = useJobs()

function num(n: unknown): string {
  return (Number(n) || 0).toLocaleString()
}

function ago(ts: unknown): string {
  const t = Number(ts)
  if (!t) return '—'
  const s = Math.max(0, Math.floor(Date.now() / 1000 - t))
  if (s < 60) return s + 's ago'
  const m = Math.floor(s / 60)
  if (m < 60) return m + 'm ago'
  const h = Math.floor(m / 60)
  if (h < 24) return h + 'h ago'
  return Math.floor(h / 24) + 'd ago'
}

function sumKinds(byKind: Record<string, number> | undefined): number {
  let t = 0
  for (const k of Object.keys(byKind || {})) t += byKind?.[k] || 0
  return t
}

function statusTotal(status: string): number {
  return sumKinds(stats.value?.review?.[status])
}

const empty = computed(() => {
  const s = stats.value
  if (!s) return false
  const synced = Object.values(s.photos || {}).reduce((a, p) => a + (p.synced || 0), 0)
  return synced === 0 && Number(s.faces || 0) === 0
})

interface Tile {
  label: string
  value: string
  sub?: string
  to?: string | { path: string; query: Record<string, string> }
  hint?: { text: string; linkText: string; to: string } // "Download models" style sub
}

const tiles = computed<Tile[]>(() => {
  const s = stats.value
  if (!s) return []
  const out: Tile[] = []

  for (const [space, p] of Object.entries(s.photos || {})) {
    const sub = num(p.hashed) + ' hashed' + (p.deleted ? ' · ' + num(p.deleted) + ' deleted' : '')
    out.push({ label: 'Photos · ' + space, value: num(p.synced), sub, to: '/pipeline' })
  }

  out.push({
    label: 'Faces',
    value: num(s.faces),
    sub: num(s.embeddings) + ' embeddings',
    to: '/pipeline',
  })

  const ex = s.extract || {}
  if (!ex.models_ready) {
    out.push({
      label: 'Photos scanned',
      value: '—',
      hint: { text: ' to enable face detection', linkText: 'Download models', to: '/pipeline' },
    })
  } else {
    const pct = ex.coverage != null ? Math.round(ex.coverage * 100) + '% covered' : 'no eligible photos'
    out.push({
      label: 'Photos scanned',
      value: num(ex.processed) + ' / ' + num(ex.eligible),
      sub: pct,
      to: '/pipeline',
    })
  }

  const cl = s.cluster
  if (cl) {
    out.push({
      label: 'Face groups',
      value: num(cl.clusters),
      sub: 'run #' + cl.run_id + ' · ' + ago(cl.created_at),
      to: '/pipeline',
    })
  } else {
    out.push({ label: 'Face groups', value: '—', sub: 'not grouped yet', to: '/pipeline' })
  }

  return out
})

const reviewPending = computed(() => statusTotal('pending'))
const reviewBreakdown = computed(() =>
  (['approved', 'applied', 'rejected', 'hidden', 'failed'] as const)
    .map((st) => ({ st, n: statusTotal(st) }))
    .filter((r) => r.n > 0),
)

interface Stage {
  label: string
  to: string | { path: string; query: Record<string, string> }
  state: 'pending' | 'active' | 'done'
  note: string
}

const stages = computed<Stage[]>(() => {
  const s = stats.value
  if (!s) return []
  const synced = Object.values(s.photos || {}).reduce((a, p) => a + (p.synced || 0), 0)

  const ex = s.extract || {}
  let exState: Stage['state']
  let exNote: string
  if (!ex.models_ready) {
    exState = 'pending'
    exNote = 'models needed'
  } else if (ex.eligible > 0 && ex.processed != null && ex.processed >= ex.eligible) {
    exState = 'done'
    exNote = 'complete'
  } else if ((ex.processed ?? 0) > 0) {
    exState = 'active'
    exNote = num(ex.processed) + ' / ' + num(ex.eligible)
  } else {
    exState = 'pending'
    exNote = 'not started'
  }

  const cl = s.cluster
  const pending = statusTotal('pending')
  const approved = statusTotal('approved')
  const applied = statusTotal('applied')
  // Hidden items are reviewed items — a human decided "never again" — so they
  // count toward the queue being worked through.
  const totalItems =
    pending +
    approved +
    applied +
    statusTotal('rejected') +
    statusTotal('hidden') +
    statusTotal('failed')

  let revState: Stage['state']
  let revNote: string
  if (totalItems === 0) {
    revState = 'pending'
    revNote = 'no items'
  } else if (pending > 0) {
    revState = 'active'
    revNote = num(pending) + ' pending'
  } else {
    revState = 'done'
    revNote = 'all reviewed'
  }

  let apState: Stage['state']
  let apNote: string
  if (approved > 0) {
    apState = 'active'
    apNote = num(approved) + ' approved'
  } else if (applied > 0) {
    apState = 'done'
    apNote = num(applied) + ' applied'
  } else {
    apState = 'pending'
    apNote = 'nothing to apply'
  }

  return [
    {
      label: 'Sync',
      to: '/pipeline',
      state: synced > 0 ? 'done' : 'pending',
      note: synced > 0 ? num(synced) + ' synced' : 'not started',
    },
    { label: 'Detect faces', to: '/pipeline', state: exState, note: exNote },
    {
      label: 'Group faces',
      to: '/pipeline',
      state: cl ? 'done' : 'pending',
      note: cl ? num(cl.clusters) + ' groups' : 'not started',
    },
    { label: 'Review', to: '/review', state: revState, note: revNote },
    { label: 'Apply', to: '/apply', state: apState, note: apNote },
  ]
})

function auditWhen(it: AuditEntry): string {
  return it.ts ? new Date(Number(it.ts) * 1000).toLocaleString() : '—'
}
function auditSuccess(it: AuditEntry): 1 | 0 | -1 {
  if (it.success === 1 || it.success === true) return 1
  if (it.success === 0 || it.success === false) return 0
  return -1
}

async function pullStats(): Promise<void> {
  const [s, a] = await Promise.all([
    getJSON<Stats>('/api/stats'),
    getJSON<{ items: AuditEntry[] }>('/api/audit?limit=20'),
  ])
  stats.value = s
  audit.value = a.items || []
}

onMounted(async () => {
  try {
    await pullStats()
  } catch {
    /* client handles 401; leave the page in its loading state */
  } finally {
    loaded.value = true
  }
})

// Stats are expensive relative to the job list, so refresh them only when the
// shared store reports a job — and once more when that job finishes, to pick up
// whatever it wrote.
watch(
  () => jobs.running?.id ?? null,
  (now, before) => {
    if (now !== null || before !== null) void pullStats().catch(() => {})
  },
)
</script>

<template>
  <div class="page dashboard-page">
    <div v-if="empty" class="card placeholder-card">
      <h2>No data yet</h2>
      <p class="muted">
        Synopticon has not synced anything from your NAS. Run your first sync to pull the photo
        library, persons and faces, then detect and group faces.
      </p>
      <div class="cta-row">
        <RouterLink class="btn btn-action" to="/pipeline">Run your first sync</RouterLink>
        <RouterLink class="btn btn-ghost" to="/setup">Open the setup wizard</RouterLink>
      </div>
    </div>

    <template v-else-if="stats">
      <section class="stat-tiles" aria-label="Library statistics">
        <template v-for="tile in tiles" :key="tile.label">
          <RouterLink v-if="tile.to" class="tile" :to="tile.to">
            <div class="tile-label">{{ tile.label }}</div>
            <div class="tile-value">{{ tile.value }}</div>
            <div v-if="tile.sub" class="tile-sub">{{ tile.sub }}</div>
          </RouterLink>
          <div v-else class="tile">
            <div class="tile-label">{{ tile.label }}</div>
            <div class="tile-value">{{ tile.value }}</div>
            <div v-if="tile.hint" class="tile-hint">
              <RouterLink :to="tile.hint.to">{{ tile.hint.linkText }}</RouterLink
              >{{ tile.hint.text }}
            </div>
            <div v-else-if="tile.sub" class="tile-sub">{{ tile.sub }}</div>
          </div>
        </template>

        <!-- Review queue tile has a breakdown row of decided-status links. -->
        <RouterLink class="tile" :to="{ path: '/review', query: { status: 'pending' } }">
          <div class="tile-label">Review queue</div>
          <div class="tile-value">{{ num(reviewPending) }}</div>
          <div class="tile-sub">pending</div>
          <div class="review-breakdown">
            <template v-if="reviewBreakdown.length">
              <RouterLink
                v-for="r in reviewBreakdown"
                :key="r.st"
                :to="{ path: '/review', query: { status: r.st } }"
                >{{ r.st }} {{ num(r.n) }}</RouterLink
              >
            </template>
            <span v-else class="muted">no decisions yet</span>
          </div>
        </RouterLink>
      </section>

      <section class="pipeline-strip card" aria-label="Pipeline status">
        <h3 class="sr-only">Pipeline status</h3>
        <div class="strip">
          <template v-for="(s, i) in stages" :key="s.label">
            <span v-if="i > 0" class="stage-sep" aria-hidden="true">›</span>
            <RouterLink class="stage" :class="s.state" :to="s.to">
              <span class="stage-name"
                ><span class="stage-dot" aria-hidden="true"></span>{{ s.label }}</span
              >
              <span class="stage-note">{{ s.note }}</span>
            </RouterLink>
          </template>
        </div>
      </section>

      <section class="card audit-card">
        <h3>Recent activity</h3>
        <div>
          <p v-if="!audit.length" class="muted">No writes recorded yet.</p>
          <table v-else class="data">
            <thead>
              <tr>
                <th>When</th>
                <th>Action</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(it, i) in audit" :key="i">
                <td>{{ auditWhen(it) }}</td>
                <td>{{ it.action || '' }}</td>
                <td v-if="auditSuccess(it) === 1" class="audit-ok">✓</td>
                <td v-else-if="auditSuccess(it) === 0" class="audit-fail">✗</td>
                <td v-else>—</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <div v-else-if="loaded" class="card">
      <p class="muted">Loading…</p>
    </div>
  </div>
</template>

<style scoped>
.stat-tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: var(--sp-3);
}
.tile {
  display: block;
  background: var(--bg-raised);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-lg);
  padding: var(--sp-4);
  box-shadow: var(--shadow-card);
  color: var(--text);
}
a.tile:hover {
  text-decoration: none;
  border-color: var(--border);
}
.tile-label {
  font-size: var(--fs-sm);
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.tile-value {
  font-size: 28px;
  font-weight: 600;
  margin-top: var(--sp-1);
  line-height: 1.1;
}
.tile-sub {
  font-size: var(--fs-sm);
  color: var(--text-2);
  margin-top: 2px;
}
.tile-hint {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.review-breakdown {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
  font-size: var(--fs-sm);
}

.pipeline-strip {
  margin-top: var(--sp-4);
}
.strip {
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
}
.stage {
  flex: 1 1 140px;
  min-width: 130px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius);
  color: var(--text);
}
.stage:hover {
  background: var(--bg-sunken);
  text-decoration: none;
}
.stage-name {
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.stage-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--text-3);
  flex: 0 0 auto;
}
.stage.done .stage-dot {
  background: var(--ok);
}
.stage.active .stage-dot {
  background: var(--action);
}
.stage-note {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.stage-sep {
  align-self: center;
  color: var(--text-3);
  padding: 0 var(--sp-1);
  font-size: var(--fs-lg);
}

.audit-card {
  margin-top: var(--sp-4);
}
.audit-ok {
  color: var(--ok);
  font-weight: 600;
}
.audit-fail {
  color: var(--danger);
  font-weight: 600;
}
.placeholder-card {
  max-width: 640px;
}
</style>
