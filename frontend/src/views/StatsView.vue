<script setup lang="ts">
// Stats: what Synopticon has done over its lifetime (the run ledger + audit log)
// and what the library looks like now. The two halves load independently — the
// library scan is the slow one — and both refetch when a job finishes, taking
// that signal from the shared jobs store rather than a timer of our own.
import { computed, onMounted, shallowReactive, watch } from 'vue'
import { getJSON } from '../api/client'
import { toast } from '../stores/toasts'
import { useJobs } from '../stores/jobs'
import { commandLabel } from '../utils/jobs'
import { ago } from '../utils/time'
import {
  num,
  pct,
  fmtBytes,
  fmtDuration,
  fmtDate,
  fmtMonthOf,
  fmtTakenDate,
  fmtDay,
  fmtMonth,
} from '../utils/format'
import StatTile from '../components/stats/StatTile.vue'
import BarList, { type BarItem, type BarSegment, type BarTone } from '../components/stats/BarList.vue'
import ColumnChart, { type Column } from '../components/stats/ColumnChart.vue'
import type { LifetimeStats, LibraryStats } from '../api/types'

interface Section<T> {
  data: T | null
  loading: boolean
  error: string
}

function useSection<T>(url: string, what: string) {
  const s = shallowReactive<Section<T>>({ data: null, loading: false, error: '' })
  async function load(): Promise<void> {
    if (s.loading) return
    s.loading = true
    try {
      s.data = await getJSON<T>(url)
      s.error = ''
    } catch (e) {
      const msg = (e as Error).message || `Could not load ${what}`
      s.error = msg
      toast(msg, 'error')
    } finally {
      s.loading = false
    }
  }
  return { s, load }
}

const { s: life, load: loadLife } = useSection<LifetimeStats>('/api/stats/lifetime', 'lifetime stats')
const { s: lib, load: loadLib } = useSection<LibraryStats>('/api/stats/library', 'library stats')
const { state: jobs } = useJobs()

function refresh(): void {
  void loadLife()
  void loadLib()
}

onMounted(refresh)

// A job that just left queued/running has written whatever it is going to.
watch(
  () => jobs.running?.id ?? null,
  (now, before) => {
    if (before !== null && now !== before) refresh()
  },
)

// -- shared bits ------------------------------------------------------------

type Part = { text: string; strong?: boolean; to?: string | null; href?: string | null }
interface Fact {
  key: string
  parts: Part[]
}

function plural(n: number, one: string, many = one + 's'): string {
  return `${num(n)} ${n === 1 ? one : many}`
}

function monthYear(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
}

function argmax(xs: number[]): number {
  let best = 0
  xs.forEach((v, i) => {
    if (v > xs[best]) best = i
  })
  return best
}

const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0)

// -- lifetime ---------------------------------------------------------------

const lifeEmpty = computed(() => {
  const l = life.data
  if (!l) return false
  return l.runs.total === 0 && l.nas.writes_ok + l.nas.writes_failed === 0 && l.review.decided === 0
})

const lifeFacts = computed<Fact[]>(() => {
  const l = life.data
  if (!l) return []
  const out: Fact[] = []
  if (l.work.faces_found > 0) {
    out.push({
      key: 'faces',
      parts: [
        { text: 'Synopticon has found ' },
        { text: num(l.work.faces_found), strong: true },
        { text: ` faces${l.tracking_since ? ' since ' + monthYear(l.tracking_since) : ''}.` },
      ],
    })
  }
  if (l.nas.writes_ok > 0) {
    out.push({
      key: 'writes',
      parts: [
        { text: 'It has made ' },
        { text: plural(l.nas.writes_ok, 'change'), strong: true },
        { text: ` to your NAS${l.first_nas_write ? ' since ' + fmtDate(l.first_nas_write) : ''}.` },
      ],
    })
  }
  if (l.runs.total_seconds > 0) {
    out.push({
      key: 'time',
      parts: [
        { text: 'Time spent working: ' },
        { text: fmtDuration(l.runs.total_seconds), strong: true },
        { text: ` over ${plural(l.runs.active_days, 'active day')}.` },
      ],
    })
  }
  if (l.runs.longest) {
    out.push({
      key: 'longest',
      parts: [
        { text: 'Longest run: ' },
        { text: commandLabel(l.runs.longest.command), strong: true },
        {
          text: `, ${fmtDuration(l.runs.longest.seconds)} on ${fmtDate(l.runs.longest.started_at)}.`,
        },
      ],
    })
  }
  if (l.runs.busiest_day) {
    out.push({
      key: 'busiest',
      parts: [
        { text: 'Busiest day: ' },
        { text: fmtDay(l.runs.busiest_day.date), strong: true },
        { text: `, with ${plural(l.runs.busiest_day.runs, 'run')}.` },
      ],
    })
  }
  if (l.runs.streak_days >= 2) {
    out.push({
      key: 'streak',
      parts: [
        { text: 'Currently on a ' },
        { text: `${num(l.runs.streak_days)}-day`, strong: true },
        { text: ' streak.' },
      ],
    })
  }
  if (l.review.approval_rate != null) {
    out.push({
      key: 'approval',
      parts: [
        { text: 'You approved ' },
        { text: pct(l.review.approval_rate), strong: true },
        { text: ' of the suggestions you reviewed.' },
      ],
    })
  }
  return out
})

const lifeTiles = computed(() => {
  const l = life.data
  if (!l) return []
  const r = l.runs
  const runSub = [
    `${num(r.ok)} ok`,
    `${num(r.failed)} failed`,
    r.cancelled ? `${num(r.cancelled)} cancelled` : '',
  ]
    .filter(Boolean)
    .join(' · ')
  return [
    {
      label: 'Tracking since',
      value: fmtMonthOf(l.tracking_since),
      sub: `${fmtDate(l.tracking_since)} · ${plural(r.active_days, 'active day')}`,
    },
    { label: 'Runs', value: num(r.total), sub: runSub },
    { label: 'Time spent', value: fmtDuration(r.total_seconds), sub: 'of finished runs' },
    {
      label: 'Faces found',
      value: num(l.work.faces_found),
      sub: `in ${plural(l.work.photos_scanned, 'photo scan')}`,
    },
    {
      label: 'NAS changes',
      value: num(l.nas.writes_ok),
      sub: `${num(l.nas.writes_failed)} failed · ${plural(l.nas.dry_runs, 'dry run')}`,
    },
    {
      label: 'Review decisions',
      value: num(l.review.decided),
      sub:
        l.review.approval_rate != null
          ? `${pct(l.review.approval_rate)} approved`
          : 'none yet',
    },
  ]
})

const RUN_LEGEND: Array<{ tone: BarTone; label: string }> = [
  { tone: 'data', label: 'Succeeded' },
  { tone: 'bad', label: 'Failed' },
  { tone: 'rest', label: "Cancelled or didn't finish" },
]

const runItems = computed<BarItem[]>(() =>
  (life.data?.runs.by_command ?? []).map((c): BarItem => {
    const notes = [c.last_run ? 'last ' + ago(c.last_run) : '', c.seconds ? fmtDuration(c.seconds) + ' total' : '']
    return {
      key: c.command,
      label: commandLabel(c.command),
      value: c.total,
      note: notes.filter(Boolean).join(' · '),
      segments: [
        { value: c.ok, tone: 'data', label: 'succeeded' },
        { value: c.failed, tone: 'bad', label: 'failed' },
        { value: c.cancelled + c.unfinished, tone: 'rest', label: "cancelled or didn't finish" },
      ],
    }
  }),
)

const runLegend = computed(() => {
  const r = life.data?.runs
  if (!r) return []
  return RUN_LEGEND.filter(
    (l) =>
      (l.tone === 'data' && r.ok) ||
      (l.tone === 'bad' && r.failed) ||
      (l.tone === 'rest' && r.cancelled + r.unfinished),
  )
})

const originItems = computed<BarItem[]>(() => {
  const o = life.data?.runs.by_origin
  if (!o) return []
  return [
    { key: 'cli', label: 'Command line', value: o.cli },
    { key: 'gui', label: 'This web GUI', value: o.gui },
    { key: 'schedule', label: 'A schedule', value: o.schedule },
  ]
})

const ACTION_LABELS: Record<string, string> = {
  'assign.add_face': 'Tag a face',
  'assign.upload_face': 'Tag a face (new box)',
  'assign.verify': 'Check a tag landed',
  'reassign.separate': 'Move a face',
  'reassign.verify': 'Check a move landed',
  merge: 'Merge people',
  rename: 'Name a person',
  set_show: 'Show or hide a person',
  delete_face: 'Remove a detection',
  'dedupe.delete': 'Delete a duplicate photo',
}

/** Audit actions are `<surface>.<what>`; the surface is shown only when it is not the pipeline. */
function actionLabel(action: string): string {
  if (ACTION_LABELS[action]) return ACTION_LABELS[action]
  const dot = action.indexOf('.')
  const surface = dot > 0 ? action.slice(0, dot) : ''
  const rest = dot > 0 ? action.slice(dot + 1) : action
  const base =
    ACTION_LABELS[rest] ??
    rest.replace(/[._]/g, ' ').replace(/^./, (ch) => ch.toUpperCase())
  if (surface === 'quickmerger') return base + ' (QuickMerger)'
  return base
}

const nasItems = computed<BarItem[]>(() =>
  (life.data?.nas.by_action ?? []).map((a): BarItem => ({
    key: a.action,
    label: actionLabel(a.action),
    value: a.ok + a.failed,
    valueText: a.failed ? `${num(a.ok)} · ${num(a.failed)} failed` : num(a.ok),
    segments: [
      { value: a.ok, tone: 'data', label: 'succeeded' },
      { value: a.failed, tone: 'bad', label: 'failed' },
    ],
  })),
)

const nasHighlights = computed(() => {
  const n = life.data?.nas
  if (!n) return []
  return [
    { label: 'Faces tagged', value: n.faces_tagged },
    { label: 'Faces moved', value: n.faces_moved },
    { label: 'People merged', value: n.merges },
    { label: 'People named', value: n.people_named },
    { label: 'Duplicates deleted', value: n.duplicates_deleted },
  ]
})

const workRows = computed(() => {
  const w = life.data?.work
  if (!w) return []
  return [
    { label: 'Photos synced', value: w.photos_synced },
    { label: 'Photos scanned for faces', value: w.photos_scanned },
    { label: 'Faces found', value: w.faces_found },
    { label: 'Faces sharpened by restoration', value: w.faces_restored },
    { label: 'Photos fingerprinted', value: w.photos_hashed },
    { label: 'Crops regenerated', value: w.crops_regenerated },
    { label: 'Duplicate groups found', value: w.duplicate_groups_found },
    { label: 'Broken review items removed', value: w.queue_items_pruned },
  ]
})

const reviewItems = computed<BarItem[]>(() => {
  const r = life.data?.review
  if (!r) return []
  return [
    { key: 'approved', label: 'Approved', value: r.approved },
    { key: 'rejected', label: 'Rejected', value: r.rejected },
    { key: 'hidden', label: 'Hidden for good', value: r.hidden },
  ]
})

// -- library ----------------------------------------------------------------

const WEEKDAYS = Array.from({ length: 7 }, (_, i) =>
  // 2024-01-01 was a Monday, matching the API's Monday-first order.
  new Date(Date.UTC(2024, 0, 1 + i)),
)
const weekdayLong = WEEKDAYS.map((d) => d.toLocaleDateString(undefined, { weekday: 'long', timeZone: 'UTC' }))
const weekdayShort = WEEKDAYS.map((d) => d.toLocaleDateString(undefined, { weekday: 'short', timeZone: 'UTC' }))

function hourLabel(h: number): string {
  return `${String(h).padStart(2, '0')}:00`
}

const libEmpty = computed(() => (lib.data ? lib.data.photos.total === 0 : false))

const libFacts = computed<Fact[]>(() => {
  const d = lib.data
  if (!d) return []
  const out: Fact[] = []
  const mc = d.people.main_character
  if (mc) {
    out.push({
      key: 'main',
      parts: [
        { text: 'Your main character is ' },
        { text: mc.name, strong: true, href: mc.person_url },
        { text: ` — ${pct(mc.share)} of every tagged face.` },
      ],
    })
  }
  if (sum(d.photos.by_hour) > 0) {
    out.push({
      key: 'hour',
      parts: [
        { text: 'Golden hour: most photos are taken around ' },
        { text: hourLabel(argmax(d.photos.by_hour)), strong: true },
        { text: '.' },
      ],
    })
  }
  if (sum(d.photos.by_weekday) > 0) {
    out.push({
      key: 'weekday',
      parts: [
        { text: 'Favourite day to take pictures: ' },
        { text: weekdayLong[argmax(d.photos.by_weekday)], strong: true },
        { text: '.' },
      ],
    })
  }
  if (d.photos.busiest_day) {
    out.push({
      key: 'busiest',
      parts: [
        { text: 'Busiest day ever: ' },
        { text: fmtDay(d.photos.busiest_day.date), strong: true },
        { text: `, with ${plural(d.photos.busiest_day.count, 'photo')}.` },
      ],
    })
  }
  if (d.photos.oldest) {
    out.push({
      key: 'oldest',
      parts: [
        { text: 'Your oldest photo was taken on ' },
        { text: fmtTakenDate(d.photos.oldest.time), strong: true, to: d.photos.oldest.inspect },
        { text: '.' },
      ],
    })
  }
  if (d.faces.most_crowded && d.faces.most_crowded.faces > 1) {
    out.push({
      key: 'crowded',
      parts: [
        { text: 'The most crowded photo has ' },
        { text: plural(d.faces.most_crowded.faces, 'face'), strong: true, to: d.faces.most_crowded.inspect },
        { text: ' in one frame.' },
      ],
    })
  }
  return out
})

const span = computed(() => {
  const p = lib.data?.photos
  if (!p?.oldest || !p.newest) return null
  const y = (t: number) => new Date(t * 1000).getUTCFullYear()
  const a = y(p.oldest.time)
  const b = y(p.newest.time)
  return {
    value: num(b - a + 1),
    sub: `${fmtTakenDate(p.oldest.time)} to ${fmtTakenDate(p.newest.time)}`,
  }
})

const libTiles = computed(() => {
  const d = lib.data
  if (!d) return []
  const p = d.photos
  const f = d.faces
  const tiles: Array<{ label: string; value: string; sub?: string; to?: string }> = [
    {
      label: 'Photos',
      value: num(p.total),
      sub: fmtBytes(p.bytes) + (p.deleted ? ` · ${num(p.deleted)} deleted` : ''),
    },
  ]
  if (span.value) tiles.push({ label: 'Years of photos', value: span.value.value, sub: span.value.sub })
  tiles.push(
    {
      label: 'Faces detected',
      value: num(f.detected),
      sub: `${num(f.synology_tagged)} tagged by Synology`,
    },
    {
      label: 'Named people',
      value: num(d.people.listed_named),
      sub: d.people.hidden ? `${num(d.people.hidden)} hidden in Synology` : undefined,
    },
    {
      label: 'Nobody in them',
      value: num(f.photos_without_faces),
      sub: f.photos_scanned
        ? `${pct(f.photos_without_faces / f.photos_scanned)} of scanned photos`
        : 'not scanned yet',
    },
    {
      label: 'Duplicate copies',
      value: num(p.duplicate_copies),
      sub: 'byte-identical extras',
      to: p.duplicate_copies ? '/utilities' : undefined,
    },
  )
  return tiles
})

const yearColumns = computed<Column[]>(() =>
  (lib.data?.photos.per_year ?? []).map((y) => ({ label: String(y.year), value: y.count })),
)

const hourColumns = computed<Column[]>(() =>
  (lib.data?.photos.by_hour ?? []).map((v, h) => ({
    label: `${hourLabel(h)}–${String(h).padStart(2, '0')}:59`,
    axis: String(h),
    value: v,
  })),
)

const weekdayColumns = computed<Column[]>(() =>
  (lib.data?.photos.by_weekday ?? []).map((v, i) => ({
    label: weekdayLong[i] ?? String(i),
    axis: weekdayShort[i] ?? String(i),
    value: v,
  })),
)

const growthColumns = computed<Column[]>(() =>
  (lib.data?.photos.growth ?? []).map((g) => ({ label: fmtMonth(g.month), value: g.total })),
)

const TYPE_LABELS: Record<string, string> = {
  photo: 'Photos',
  video: 'Videos',
  live: 'Live photos',
  other: 'Other',
}

const typeItems = computed<BarItem[]>(() => {
  const t = lib.data?.photos.by_type
  if (!t) return []
  return (Object.keys(TYPE_LABELS) as Array<keyof typeof t>)
    .map((k) => ({ key: k, label: TYPE_LABELS[k], value: t[k] ?? 0 }))
    .filter((i) => i.value > 0)
})

function spaceLabel(space: string): string {
  if (space === 'personal') return 'Personal space'
  if (space === 'shared') return 'Shared space'
  return space
}

const spaceItems = computed<BarItem[]>(() =>
  Object.entries(lib.data?.photos.by_space ?? {}).map(([k, v]) => ({
    key: k,
    label: spaceLabel(k),
    value: v,
  })),
)

const faceItems = computed<BarItem[]>(() => {
  const f = lib.data?.faces
  if (!f) return []
  return [
    { key: 'tagged', label: 'Tagged in Synology Photos', value: f.synology_tagged },
    { key: 'named', label: '…on a named person', value: f.synology_named },
    { key: 'detected', label: 'Detected by Synopticon', value: f.detected },
    { key: 'restored', label: 'Sharpened by restoration', value: f.restored },
  ]
})

const perPhotoColumns = computed<Column[]>(() =>
  (lib.data?.faces.per_photo ?? []).map((b) => ({
    label: `${b.bucket} ${b.bucket === '1' ? 'face' : 'faces'}`,
    axis: b.bucket,
    value: b.photos,
  })),
)

const DETECTOR_LABELS: Record<string, string> = {
  scrfd: 'SCRFD only',
  yolo: 'YOLO only',
  merged: 'Both detectors agreed',
}

const detectorItems = computed<BarItem[]>(() => {
  const dc = lib.data?.faces.detector_counts ?? {}
  const total = sum(Object.values(dc))
  return Object.entries(dc)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => ({
      key: k,
      label: DETECTOR_LABELS[k] ?? k,
      value: v,
      valueText: `${num(v)} · ${pct(total ? v / total : 0)}`,
    }))
})

const curiosities = computed(() => {
  const f = lib.data?.faces
  if (!f) return []
  const out: Array<{ label: string; value: string; to?: string | null }> = []
  if (f.avg_per_photo != null) out.push({ label: 'Faces per photo, on average', value: f.avg_per_photo.toFixed(2) })
  if (f.most_crowded) {
    out.push({ label: 'Most crowded photo', value: plural(f.most_crowded.faces, 'face'), to: f.most_crowded.inspect })
  }
  if (f.tiniest) {
    out.push({
      label: 'Tiniest face',
      value: `${num(f.tiniest.width)} × ${num(f.tiniest.height)} px`,
      to: f.tiniest.inspect,
    })
  }
  if (f.sharpest_eye) {
    out.push({
      label: 'Most confident detection',
      value: (f.sharpest_eye.det_score * 100).toFixed(1) + '% sure',
      to: f.sharpest_eye.inspect,
    })
  }
  return out
})

const peopleItems = computed<BarItem[]>(() =>
  (lib.data?.people.distribution ?? []).map((d) => ({
    key: `${d.space}:${d.person_id}`,
    label: d.name,
    href: d.person_url ?? undefined,
    value: d.faces,
    valueText: `${num(d.faces)} · ${pct(d.share)}`,
  })),
)

/** One full-width bar of every tagged face, so the named people's bars can scale to the top person. */
const peopleWhole = computed(() => {
  const p = lib.data?.people
  const tagged = lib.data?.faces.synology_tagged ?? 0
  if (!p || !tagged) return null
  const shown = p.distribution.length
  const top = p.distribution.reduce((sum, d) => sum + d.faces, 0)
  const segments: BarSegment[] = [
    { value: top, tone: 'data', label: `top ${shown}` },
    {
      value: p.everyone_else.faces,
      tone: 'rest',
      label: `everyone else (${plural(p.everyone_else.people, 'person', 'people')})`,
    },
    { value: p.unnamed_or_unlisted_faces, tone: 'faint', label: 'unnamed or unlisted' },
  ]
  const item: BarItem = {
    key: 'whole',
    label: 'Every tagged face',
    value: tagged,
    valueText: num(tagged),
    segments,
  }
  const legend = segments
    .filter((seg) => seg.value > 0)
    .map((seg) => ({ tone: seg.tone, label: `${seg.label} · ${pct(seg.value / tagged)}` }))
  return { items: [item], legend }
})

const concentration = computed(() => {
  const g = lib.data?.people.gini
  if (g == null) return ''
  if (g < 0.35) return 'Faces are spread fairly evenly across your people.'
  if (g < 0.6) return 'A handful of people show up a lot more than the rest.'
  return 'A few people dominate your library.'
})

const groupingRows = computed(() => {
  const g = lib.data?.grouping
  if (!g) return []
  return [
    { label: 'Face groups', value: num(g.clusters) },
    { label: 'Largest group', value: plural(g.largest, 'face') },
    { label: 'Single-face groups', value: num(g.singletons) },
    { label: 'Matched to a person', value: num(g.mapped) },
    { label: 'Not matched yet', value: num(g.unmapped) },
  ]
})
</script>

<template>
  <div class="page stats-page">
    <!-- ================= What Synopticon has done ================= -->
    <section class="stats-section" aria-labelledby="life-h" :class="{ refreshing: life.loading && life.data }">
      <header class="section-head">
        <h2 id="life-h">What Synopticon has done</h2>
        <p v-if="life.data?.tracking_since" class="muted">
          Counting since {{ fmtDate(life.data.tracking_since) }}
        </p>
      </header>

      <div v-if="!life.data && life.loading" class="tiles" aria-busy="true">
        <div v-for="i in 6" :key="i" class="skeleton tile-skel"></div>
      </div>

      <div v-else-if="!life.data && life.error" class="card">
        <p class="muted">Couldn't load these numbers: {{ life.error }}</p>
        <button type="button" class="btn btn-sm" @click="loadLife">Try again</button>
      </div>

      <div v-else-if="lifeEmpty" class="card empty-card">
        <h3>Nothing to count yet</h3>
        <p class="muted">
          Synopticon keeps a tally of every command it runs and every change it writes to your NAS. Run a sync from
          the Pipeline page and this fills in.
        </p>
        <div class="cta-row">
          <RouterLink class="btn btn-action" to="/pipeline">Open the Pipeline</RouterLink>
        </div>
      </div>

      <template v-else-if="life.data">
        <ul v-if="lifeFacts.length" class="facts card">
          <li v-for="f in lifeFacts" :key="f.key">
            <template v-for="(p, i) in f.parts" :key="i">
              <RouterLink v-if="p.to" :to="p.to" class="fact-strong">{{ p.text }}</RouterLink>
              <a v-else-if="p.href" :href="p.href" target="_blank" rel="noopener" class="fact-strong">{{
                p.text
              }}</a>
              <strong v-else-if="p.strong" class="fact-strong">{{ p.text }}</strong>
              <span v-else>{{ p.text }}</span>
            </template>
          </li>
        </ul>

        <div class="tiles">
          <StatTile v-for="t in lifeTiles" :key="t.label" :label="t.label" :value="t.value" :sub="t.sub" />
        </div>

        <div class="two-col">
          <section class="card">
            <h3>Runs by operation</h3>
            <BarList
              v-if="runItems.length"
              :items="runItems"
              :legend="runLegend"
              label="Runs per operation, split by outcome"
            />
            <p v-else class="muted">No runs recorded yet.</p>

            <h4 class="sub-head">Started from</h4>
            <BarList :items="originItems" label="Where runs were started" />
          </section>

          <section class="card">
            <h3>Written to your NAS</h3>
            <template v-if="life.data.nas.writes_ok + life.data.nas.writes_failed > 0">
              <dl class="kv kv-grid">
                <div v-for="h in nasHighlights" :key="h.label">
                  <dt>{{ h.label }}</dt>
                  <dd>{{ num(h.value) }}</dd>
                </div>
              </dl>
              <h4 class="sub-head">Every change, by kind</h4>
              <BarList
                :items="nasItems"
                :legend="
                  life.data.nas.writes_failed
                    ? [
                        { tone: 'data', label: 'Succeeded' },
                        { tone: 'bad', label: 'Failed' },
                      ]
                    : undefined
                "
                label="NAS changes by kind"
              />
            </template>
            <p v-else class="muted">
              Nothing written yet — everything before Apply only reads from your NAS.
              <template v-if="life.data.nas.dry_runs">
                {{ plural(life.data.nas.dry_runs, 'dry run') }} so far.
              </template>
            </p>
          </section>

          <section class="card">
            <h3>Behind the scenes</h3>
            <p class="muted card-lede">Lifetime totals, re-scans included.</p>
            <dl class="kv">
              <div v-for="w in workRows" :key="w.label">
                <dt>{{ w.label }}</dt>
                <dd>{{ num(w.value) }}</dd>
              </div>
            </dl>
          </section>

          <section class="card">
            <h3>Your review decisions</h3>
            <template v-if="life.data.review.decided > 0">
              <BarList :items="reviewItems" label="Review decisions" />
              <p v-if="life.data.review.approval_rate != null" class="muted card-foot">
                {{ pct(life.data.review.approval_rate) }} of suggestions approved, out of
                {{ plural(life.data.review.decided, 'decision') }}.
              </p>
            </template>
            <p v-else class="muted">
              No decisions yet. Suggestions show up on the <RouterLink to="/review">Review</RouterLink> page after
              faces are grouped.
            </p>
          </section>
        </div>
      </template>
    </section>

    <!-- ======================== Your library ======================== -->
    <section class="stats-section" aria-labelledby="lib-h" :class="{ refreshing: lib.loading && lib.data }">
      <header class="section-head">
        <h2 id="lib-h">Your library</h2>
        <p v-if="lib.data" class="muted">As of {{ new Date(lib.data.computed_at * 1000).toLocaleString() }}</p>
      </header>

      <div v-if="!lib.data && lib.loading" aria-busy="true">
        <div class="tiles">
          <div v-for="i in 6" :key="i" class="skeleton tile-skel"></div>
        </div>
        <div class="skeleton chart-skel"></div>
      </div>

      <div v-else-if="!lib.data && lib.error" class="card">
        <p class="muted">Couldn't load library stats: {{ lib.error }}</p>
        <button type="button" class="btn btn-sm" @click="loadLib">Try again</button>
      </div>

      <div v-else-if="libEmpty" class="card empty-card">
        <h3>No photos synced yet</h3>
        <p class="muted">
          Once Synopticon has synced your library from the NAS, this is where you find out who your main character
          is.
        </p>
        <div class="cta-row">
          <RouterLink class="btn btn-action" to="/pipeline">Run your first sync</RouterLink>
        </div>
      </div>

      <template v-else-if="lib.data">
        <ul v-if="libFacts.length" class="facts card">
          <li v-for="f in libFacts" :key="f.key">
            <template v-for="(p, i) in f.parts" :key="i">
              <RouterLink v-if="p.to" :to="p.to" class="fact-strong">{{ p.text }}</RouterLink>
              <a v-else-if="p.href" :href="p.href" target="_blank" rel="noopener" class="fact-strong">{{
                p.text
              }}</a>
              <strong v-else-if="p.strong" class="fact-strong">{{ p.text }}</strong>
              <span v-else>{{ p.text }}</span>
            </template>
          </li>
        </ul>

        <div class="tiles">
          <StatTile
            v-for="t in libTiles"
            :key="t.label"
            :label="t.label"
            :value="t.value"
            :sub="t.sub"
            :to="t.to"
          />
        </div>

        <h3 class="group-head">Photos</h3>
        <section class="card">
          <h3>Photos per year</h3>
          <p class="muted card-lede">By the date each photo was taken.</p>
          <ColumnChart :columns="yearColumns" label="Photos per year" unit="photos" />
        </section>

        <div class="two-col">
          <section class="card">
            <h3>Time of day</h3>
            <p class="muted card-lede">The hour each photo was taken.</p>
            <ColumnChart :columns="hourColumns" label="Photos by hour of day" unit="photos" />
          </section>
          <section class="card">
            <h3>Day of the week</h3>
            <p class="muted card-lede">The weekday each photo was taken.</p>
            <ColumnChart :columns="weekdayColumns" label="Photos by day of the week" unit="photos" />
          </section>
        </div>

        <div class="two-col">
          <section class="card">
            <h3>Library growth</h3>
            <p class="muted card-lede">Photos in Synology Photos over time, by when they were added.</p>
            <ColumnChart
              :columns="growthColumns"
              variant="area"
              highlight="last"
              label="Library size over time"
              unit="photos"
            />
          </section>
          <section class="card">
            <h3>What's in it</h3>
            <BarList v-if="typeItems.length" :items="typeItems" label="Library by file type" />
            <p v-else class="muted">No file types recorded.</p>
            <template v-if="spaceItems.length > 1">
              <h4 class="sub-head">By space</h4>
              <BarList :items="spaceItems" label="Library by space" />
            </template>
          </section>
        </div>

        <h3 class="group-head">Faces</h3>
        <div class="two-col">
          <section class="card">
            <h3>Faces in your library</h3>
            <BarList :items="faceItems" label="Face counts" />
            <dl v-if="curiosities.length" class="kv curiosities">
              <div v-for="c in curiosities" :key="c.label">
                <dt>{{ c.label }}</dt>
                <dd>
                  <RouterLink v-if="c.to" :to="c.to">{{ c.value }}</RouterLink>
                  <template v-else>{{ c.value }}</template>
                </dd>
              </div>
            </dl>
          </section>

          <section class="card">
            <h3>Faces per photo</h3>
            <template v-if="lib.data.faces.photos_scanned > 0">
              <p class="muted card-lede">
                Across {{ plural(lib.data.faces.photos_scanned, 'scanned photo') }}.
              </p>
              <ColumnChart
                :columns="perPhotoColumns"
                label="Photos by number of faces in them"
                unit="photos"
              />
              <template v-if="detectorItems.length">
                <h4 class="sub-head">Which detector found them</h4>
                <BarList :items="detectorItems" label="Faces by detector" />
              </template>
            </template>
            <p v-else class="muted">
              Faces haven't been detected yet. Run <strong>Detect faces</strong> from the
              <RouterLink to="/pipeline">Pipeline</RouterLink> page.
            </p>
          </section>
        </div>

        <h3 class="group-head">People</h3>
        <section class="card">
          <h3>Who shows up most</h3>
          <template v-if="lib.data.faces.synology_tagged > 0">
            <p class="muted card-lede">
              Share of every face Synology Photos has tagged, across
              {{ plural(lib.data.people.listed_named, 'named person', 'named people') }}.
            </p>
            <BarList
              v-if="peopleWhole"
              class="people-whole"
              :items="peopleWhole.items"
              :legend="peopleWhole.legend"
              label="Every tagged face, by who it belongs to"
            />
            <BarList :items="peopleItems" label="Tagged faces per person" />
            <dl class="kv kv-grid people-facts">
              <div v-if="lib.data.people.listed_named >= 10">
                <dt>Top 10 hold</dt>
                <dd>{{ pct(lib.data.people.top10_share) }}</dd>
              </div>
              <div>
                <dt>Named people with a single face</dt>
                <dd>{{ num(lib.data.people.one_face_people) }}</dd>
              </div>
              <div v-if="lib.data.people.hidden">
                <dt>Hidden in Synology</dt>
                <dd>{{ num(lib.data.people.hidden) }}</dd>
              </div>
            </dl>
            <p
              v-if="concentration"
              class="muted card-foot"
              :title="`Gini coefficient ${lib.data.people.gini?.toFixed(2)}`"
            >
              {{ concentration }}
            </p>
          </template>
          <p v-else class="muted">Synology Photos hasn't tagged any faces yet.</p>
        </section>

        <section class="card">
          <h3>Latest face grouping</h3>
          <template v-if="lib.data.grouping">
            <p class="muted card-lede">
              Run #{{ lib.data.grouping.run_id }} · {{ ago(lib.data.grouping.created_at) }}
            </p>
            <dl class="kv kv-grid">
              <div v-for="g in groupingRows" :key="g.label">
                <dt>{{ g.label }}</dt>
                <dd>{{ g.value }}</dd>
              </div>
            </dl>
          </template>
          <p v-else class="muted">
            Not grouped yet. Run <strong>Group faces</strong> from the
            <RouterLink to="/pipeline">Pipeline</RouterLink> page once faces are detected.
          </p>
        </section>
      </template>
    </section>
  </div>
</template>

<style scoped>
.stats-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  transition: opacity 0.15s;
}
.stats-section + .stats-section {
  margin-top: var(--sp-6);
}
.stats-section.refreshing {
  opacity: 0.6;
}
.section-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-1) var(--sp-3);
}
.section-head h2,
.section-head p {
  margin: 0;
}
.group-head {
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr));
  gap: var(--sp-3);
}
.tiles :deep(.tile-value) {
  white-space: nowrap;
}
.tile-skel {
  height: 92px;
  border-radius: var(--radius-lg);
}
.chart-skel {
  height: 220px;
  margin-top: var(--sp-4);
  border-radius: var(--radius-lg);
}
.two-col {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 380px), 1fr));
  gap: var(--sp-4);
  align-items: start;
}
.card {
  min-width: 0;
}
.card-lede {
  margin: calc(-1 * var(--sp-2)) 0 var(--sp-3);
}
.card-foot {
  margin: var(--sp-3) 0 0;
}
.sub-head {
  margin: var(--sp-4) 0 var(--sp-2);
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--text-2);
}
.empty-card {
  max-width: 640px;
}
.facts {
  list-style: none;
  margin: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr));
  gap: var(--sp-3) var(--sp-5);
  font-size: var(--fs-lg);
  line-height: 1.4;
}
.facts li {
  min-width: 0;
  overflow-wrap: anywhere;
}
.fact-strong {
  font-weight: 600;
}
strong.fact-strong {
  color: var(--text);
}
.kv {
  margin: 0;
  display: flex;
  flex-direction: column;
}
.kv > div {
  display: flex;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: 6px 0;
  border-bottom: 1px solid var(--border-soft);
}
.kv > div:last-child {
  border-bottom: none;
}
.kv dt {
  color: var(--text-2);
  min-width: 0;
}
.kv dd {
  margin: 0;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
}
.kv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 150px), 1fr));
  gap: var(--sp-3);
}
.kv-grid > div {
  flex-direction: column;
  justify-content: flex-start;
  gap: 2px;
  padding: 0;
  border-bottom: none;
}
.kv-grid dt {
  font-size: var(--fs-sm);
}
.kv-grid dd {
  text-align: left;
  font-size: var(--fs-xl);
}
.curiosities {
  margin-top: var(--sp-4);
}
.people-facts {
  margin-top: var(--sp-4);
}
.people-whole {
  margin-bottom: var(--sp-4);
  padding-bottom: var(--sp-3);
  border-bottom: 1px solid var(--border-soft);
}

@media (max-width: 560px) {
  .page.stats-page {
    padding: var(--sp-3);
  }
  .facts {
    font-size: var(--fs-base);
  }
  .tiles :deep(.tile-value) {
    font-size: 22px;
  }
}
</style>
