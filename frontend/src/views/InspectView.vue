<script setup lang="ts">
// Inspect: one photo, everything we know about it. The photo comes from the NAS
// thumbnail proxy and every box is drawn on top of it as a percentage of the
// rendered frame, so ours (pixel coords, normalized server-side) and Synology's
// (already 0..1) land in the same coordinate space.
//
// The photo id lives in the path (/inspect/:space/:id) so a report is linkable:
// the review cards point here instead of straight at Synology Photos, and the
// NAS link moves inside the report.
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getJSON, ApiError } from '../api/client'
import { usePanZoom } from '../composables/usePanZoom'
import type { InspectFace, InspectMeta, InspectReport } from '../api/types'

const route = useRoute()
const router = useRouter()

const stage = ref<HTMLElement | null>(null)
const pan = usePanZoom(stage)

const meta = ref<InspectMeta | null>(null)
const report = ref<InspectReport | null>(null)
const loading = ref(false)
const error = ref('')

const query = ref('')
const space = ref('personal')

const showOurs = ref(true)
const showTheirs = ref(true)
const showLandmarks = ref(false)
const selected = ref<number | null>(null)

/** Set when the served image is not shaped like the frame the boxes assume. */
const skewed = ref(false)

const spaces = computed(() => meta.value?.spaces ?? ['personal'])

function fmtTime(t: number | null | undefined): string {
  return t ? new Date(t * 1000).toLocaleString() : '—'
}

function fmtScore(n: number | null | undefined, digits = 3): string {
  return n == null ? '—' : n.toFixed(digits)
}

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return '—'
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i]
}

/**
 * Read a photo id out of whatever the user pasted: a bare id, `space/id`, a
 * Synology deep link (…/timeline/item/42069) or an Inspect link of our own.
 */
function parseTarget(raw: string): { space: string; id: number } | null {
  const text = raw.trim()
  if (!text) return null
  const deep = /(?:^|\/)(personal|shared)(?:_space)?\/(?:timeline\/item\/)?(\d+)/.exec(text)
  if (deep) return { space: deep[1], id: Number(deep[2]) }
  const item = /timeline\/item\/(\d+)/.exec(text)
  if (item) return { space: space.value, id: Number(item[1]) }
  const bare = /^\d+$/.exec(text)
  if (bare) return { space: space.value, id: Number(bare[0]) }
  return null
}

function submit(): void {
  const target = parseTarget(query.value)
  if (!target) {
    error.value = 'Enter a photo id, or paste a link to one.'
    return
  }
  error.value = ''
  void router.push(`/inspect/${target.space}/${target.id}`)
}

async function load(sp: string, id: number): Promise<void> {
  loading.value = true
  error.value = ''
  selected.value = null
  skewed.value = false
  pan.reset()
  try {
    report.value = await getJSON<InspectReport>(`/api/inspect/${sp}/${id}`)
    space.value = sp
  } catch (e: unknown) {
    report.value = null
    error.value =
      e instanceof ApiError ? e.message : (e as Error).message || 'Could not load that photo'
  } finally {
    loading.value = false
  }
}

function syncFromRoute(): void {
  const sp = route.params.space as string | undefined
  const id = route.params.photoId as string | undefined
  if (!sp || !id) {
    report.value = null
    return
  }
  query.value = String(id)
  void load(sp, Number(id))
}

/** Flags a thumbnail whose aspect ratio disagrees with the frame we measured. */
function onImageLoad(event: Event): void {
  const img = event.target as HTMLImageElement
  const display = report.value?.display
  if (!display?.width || !display?.height || !img.naturalWidth || !img.naturalHeight) return
  const shown = img.naturalWidth / img.naturalHeight
  const expected = display.width / display.height
  skewed.value = Math.abs(shown - expected) > 0.05 * expected
}

function pct(v: number): string {
  return (v * 100).toFixed(3) + '%'
}

function faceLabel(face: InspectFace): string {
  const score = face.det_score == null ? '' : ' ' + face.det_score.toFixed(2)
  return `#${face.face_id} ${face.detector}${score}`
}

const reviewFor = computed(() => {
  const map = new Map<number, string[]>()
  for (const item of report.value?.review_items ?? []) {
    for (const fid of item.face_ids) {
      const list = map.get(fid) ?? []
      list.push(`${item.kind} · ${item.status}`)
      map.set(fid, list)
    }
  }
  return map
})

/** Review rows about the photo that name no face of ours (merges, orphans). */
const otherReviewItems = computed(() =>
  (report.value?.review_items ?? []).filter((it) => it.face_ids.length === 0),
)

function select(faceId: number): void {
  selected.value = selected.value === faceId ? null : faceId
  if (selected.value === null) return
  const box = report.value?.faces.find((f) => f.face_id === faceId)?.box
  if (box) pan.focus(box.x + box.w / 2, box.y + box.h / 2)
  document.getElementById(`face-${faceId}`)?.scrollIntoView({ block: 'nearest' })
}

/** A box click that ended a pan is a pan, not a selection. */
function clickBox(faceId: number): void {
  if (!pan.dragged.value) select(faceId)
}

onMounted(async () => {
  try {
    meta.value = await getJSON<InspectMeta>('/api/inspect/meta')
    space.value = meta.value.spaces[0] ?? 'personal'
  } catch {
    // Non-fatal: the space picker just falls back to `personal`.
  }
  syncFromRoute()
})

watch(() => [route.params.space, route.params.photoId], syncFromRoute)
</script>

<template>
  <div class="page inspect-page">
    <section class="card ins-finder">
      <h3>Inspect a photo</h3>
      <p class="muted">
        Every face we detected on one photo, with its scores, alongside the faces Synology found.
      </p>
      <form class="ins-form" @submit.prevent="submit">
        <select
          v-if="spaces.length > 1"
          class="select input-sm"
          v-model="space"
          aria-label="Library"
        >
          <option v-for="s in spaces" :key="s" :value="s">{{ s }}</option>
        </select>
        <input
          class="input"
          v-model="query"
          placeholder="Photo id, or a link to one"
          aria-label="Photo id"
        />
        <button type="submit" class="btn btn-primary">Inspect</button>
      </form>
      <p v-if="error" class="ins-error">{{ error }}</p>
    </section>

    <p v-if="loading" class="muted">Loading…</p>

    <template v-if="report && !loading">
      <section class="card ins-canvas-card">
        <header class="ins-head">
          <div>
            <h3>{{ report.photo.filename || `Photo ${report.photo_id}` }}</h3>
            <p class="muted">
              {{ report.space }} · id {{ report.photo_id }} · {{ fmtTime(report.photo.time) }}
              <span v-if="report.photo.deleted"> · deleted on the NAS</span>
            </p>
          </div>
          <a
            v-if="report.nas_url"
            class="btn"
            :href="report.nas_url"
            target="_blank"
            rel="noopener"
            >View on NAS</a
          >
        </header>

        <div class="ins-toggles">
          <label class="opt-check"
            ><input type="checkbox" v-model="showOurs" /> Our faces ({{ report.faces.length }})
          </label>
          <label class="opt-check"
            ><input type="checkbox" v-model="showTheirs" /> Synology faces ({{
              report.syno_faces.length
            }})
          </label>
          <label class="opt-check"><input type="checkbox" v-model="showLandmarks" /> Landmarks</label>
        </div>

        <div
          class="ins-stage"
          ref="stage"
          :class="{ zoomed: pan.zoomed.value, dragging: pan.dragging.value }"
          :style="{ '--k': String(pan.scale.value) }"
          @wheel="pan.onWheel"
          @pointerdown="pan.onPointerDown"
          @dblclick="pan.onDoubleClick"
        >
          <div class="ins-layer" :style="{ transform: pan.transform.value }">
            <img
              class="ins-img"
              :src="report.image_url"
              :alt="report.photo.filename || 'photo'"
              draggable="false"
              @load="onImageLoad"
            />
            <template v-if="showOurs">
              <button
                v-for="face in report.faces"
                :key="face.face_id"
                v-show="face.box"
                type="button"
                class="ins-box ours"
                :class="{ sel: selected === face.face_id }"
                :style="{
                  left: pct(face.box?.x ?? 0),
                  top: pct(face.box?.y ?? 0),
                  width: pct(face.box?.w ?? 0),
                  height: pct(face.box?.h ?? 0),
                }"
                :title="faceLabel(face)"
                @click="clickBox(face.face_id)"
              >
                <span class="ins-tag">{{ faceLabel(face) }}</span>
              </button>
            </template>
            <template v-if="showLandmarks">
              <template v-for="face in report.faces" :key="'lm-' + face.face_id">
                <span
                  v-for="(p, i) in face.landmarks || []"
                  :key="i"
                  class="ins-point"
                  :style="{ left: pct(p.x), top: pct(p.y) }"
                ></span>
              </template>
            </template>
            <template v-if="showTheirs">
              <span
                v-for="sf in report.syno_faces"
                :key="'syno-' + sf.syno_face_id"
                class="ins-box theirs"
                :style="{
                  left: pct(sf.box.x),
                  top: pct(sf.box.y),
                  width: pct(sf.box.w),
                  height: pct(sf.box.h),
                }"
              >
                <span class="ins-tag">{{ sf.name || 'unnamed' }}</span>
              </span>
            </template>
          </div>

          <div class="ins-zoom" role="group" aria-label="Zoom" @pointerdown.stop @dblclick.stop>
            <span v-if="pan.zoomed.value" class="ins-zoom-level"
              >{{ Math.round(pan.scale.value * 100) }}%</span
            >
            <button
              type="button"
              class="ins-zoom-btn"
              :disabled="pan.atMax.value"
              title="Zoom in — scroll, pinch or double-click the photo"
              aria-label="Zoom in"
              @click="pan.zoomIn"
            >
              +
            </button>
            <button
              type="button"
              class="ins-zoom-btn"
              :disabled="!pan.zoomed.value"
              title="Zoom out"
              aria-label="Zoom out"
              @click="pan.zoomOut"
            >
              &minus;
            </button>
            <button
              type="button"
              class="ins-zoom-btn"
              :disabled="!pan.zoomed.value"
              title="Fit the whole photo"
              aria-label="Fit the whole photo"
              @click="pan.reset"
            >
              &#9633;
            </button>
          </div>
        </div>

        <p v-if="skewed" class="ins-warn">
          The photo the NAS returned is not shaped like the stored resolution
          ({{ report.photo.width }}×{{ report.photo.height }}, orientation
          {{ report.photo.orientation ?? '—' }}), so the boxes may be misaligned.
        </p>
      </section>

      <section class="card">
        <h3>Detected faces</h3>
        <p v-if="!report.faces.length" class="muted">
          No faces detected yet<template v-if="!report.extract"> — this photo has not been
          through Detect faces</template
          >.
        </p>
        <div class="ins-faces">
          <article
            v-for="face in report.faces"
            :key="face.face_id"
            class="ins-face"
            :id="`face-${face.face_id}`"
            :class="{ sel: selected === face.face_id }"
            @click="select(face.face_id)"
          >
            <img v-if="face.crop_url" class="ins-crop" :src="face.crop_url" alt="" loading="lazy" />
            <div v-else class="ins-crop ins-crop-missing" title="no crop on disk">no crop</div>
            <dl class="ins-facts">
              <dt>Face</dt>
              <dd class="mono">#{{ face.face_id }}</dd>
              <dt>Detector</dt>
              <dd>{{ face.detector }}</dd>
              <dt>Score</dt>
              <dd class="mono">
                {{ fmtScore(face.det_score) }}
                <span v-if="face.det_score_secondary != null" class="muted"
                  >· 2nd {{ fmtScore(face.det_score_secondary) }}</span
                >
              </dd>
              <dt>Quality</dt>
              <dd class="mono">{{ fmtScore(face.quality, 1) }}</dd>
              <dt>Box</dt>
              <dd class="mono">
                {{ Math.round(face.bbox.x) }},{{ Math.round(face.bbox.y) }} ·
                {{ Math.round(face.bbox.w) }}×{{ Math.round(face.bbox.h) }}
              </dd>
              <dt>Group</dt>
              <dd>
                <template v-if="face.cluster">
                  #{{ face.cluster.cluster_id }} ({{ face.cluster.size ?? '?' }} faces, run
                  {{ face.cluster.run_id }})
                  <template v-if="face.cluster.mapped_person_id != null">
                    →
                    <a
                      v-if="face.cluster.mapped_person_url"
                      :href="face.cluster.mapped_person_url"
                      target="_blank"
                      rel="noopener"
                      >{{ face.cluster.mapped_person_name || face.cluster.mapped_person_id }}</a
                    >
                    <template v-else>{{
                      face.cluster.mapped_person_name || face.cluster.mapped_person_id
                    }}</template>
                    <span v-if="face.cluster.vote_fraction != null" class="muted">
                      · {{ (face.cluster.vote_fraction * 100).toFixed(0) }}% vote</span
                    >
                  </template>
                </template>
                <span v-else class="muted">not grouped</span>
              </dd>
              <dt>Embeddings</dt>
              <dd>
                <span v-if="!face.embeddings.length" class="muted">none</span>
                <span v-else>{{
                  face.embeddings.map((e) => `${e.model} (${e.variant})`).join(', ')
                }}</span>
              </dd>
              <dt v-if="face.restored">Restored</dt>
              <dd v-if="face.restored" class="mono">
                disagreement {{ fmtScore(face.restore_disagreement) }}
              </dd>
              <dt>Review</dt>
              <dd>
                <span v-if="!reviewFor.get(face.face_id)" class="muted">none</span>
                <span v-else>{{ reviewFor.get(face.face_id)?.join(', ') }}</span>
              </dd>
              <dt>Pipeline</dt>
              <dd class="mono">
                {{ face.pipeline_version }}
                <span
                  v-if="meta?.pipeline_version && meta.pipeline_version !== face.pipeline_version"
                  class="muted"
                  >· stale</span
                >
              </dd>
            </dl>
          </article>
        </div>
      </section>

      <section v-if="report.syno_faces.length" class="card">
        <h3>Synology faces</h3>
        <table class="ins-table">
          <thead>
            <tr>
              <th>Face</th>
              <th>Person</th>
              <th>Box (0–1)</th>
              <th>Synced</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="sf in report.syno_faces" :key="sf.syno_face_id">
              <td class="mono">{{ sf.syno_face_id }}</td>
              <td>
                <a
                  v-if="sf.person_url"
                  :href="sf.person_url"
                  target="_blank"
                  rel="noopener"
                  >{{ sf.name || sf.person_id }}</a
                >
                <span v-else class="muted">{{ sf.name || 'unnamed' }}</span>
              </td>
              <td class="mono">
                {{ sf.box.x.toFixed(3) }}, {{ sf.box.y.toFixed(3) }} ·
                {{ sf.box.w.toFixed(3) }}×{{ sf.box.h.toFixed(3) }}
              </td>
              <td>{{ fmtTime(sf.synced_at) }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="otherReviewItems.length" class="card">
        <h3>Other review items</h3>
        <p class="muted">Queued items that name this photo but none of its current faces.</p>
        <ul class="ins-list">
          <li v-for="it in otherReviewItems" :key="it.item_id">
            <span class="mono">#{{ it.item_id }}</span> {{ it.kind }} · {{ it.status }}
            <span v-if="it.confidence != null" class="muted"
              >· {{ fmtScore(it.confidence, 2) }}</span
            >
          </li>
        </ul>
      </section>

      <section class="card ins-meta">
        <h3>Photo</h3>
        <dl class="ins-facts">
          <dt>Resolution</dt>
          <dd class="mono">
            {{ report.photo.width ?? '?' }}×{{ report.photo.height ?? '?' }}
            <span class="muted"
              >· frame used {{ report.display.width ?? '?' }}×{{ report.display.height ?? '?' }}
              · orientation {{ report.photo.orientation ?? '—' }}</span
            >
          </dd>
          <dt>Size</dt>
          <dd class="mono">{{ fmtBytes(report.photo.filesize) }}</dd>
          <dt>Type</dt>
          <dd>{{ report.photo.type || '—' }}</dd>
          <dt>Cache key</dt>
          <dd class="mono">{{ report.photo.cache_key || '—' }}</dd>
          <dt>Hashes</dt>
          <dd class="mono">
            sha256 {{ report.photo.sha256?.slice(0, 12) || '—' }} · pHash
            {{ report.photo.phash || '—' }}
          </dd>
          <dt v-if="report.photo.similar_top_pick">Similar group</dt>
          <dd v-if="report.photo.similar_top_pick" class="mono">
            top pick {{ report.photo.similar_top_pick }}
          </dd>
          <dt>Synced</dt>
          <dd>{{ fmtTime(report.photo.synced_at) }}</dd>
          <dt>Detection run</dt>
          <dd>
            <template v-if="report.extract">
              {{ fmtTime(report.extract.processed_at) }} ·
              {{ report.extract.face_count }} faces ·
              <span class="mono">{{ report.extract.pipeline_version }}</span>
              <span v-if="report.extract.stale" class="muted"> · photo changed since</span>
            </template>
            <span v-else class="muted">never</span>
          </dd>
          <dt>Thresholds</dt>
          <dd class="mono">
            SCRFD {{ report.detection.scrfd_score }} · YOLO {{ report.detection.yolo_score }} · NMS
            {{ report.detection.nms_iou }} · cross {{ report.detection.cross_iou }} · min
            {{ report.detection.min_face_px }}px
          </dd>
        </dl>
      </section>
    </template>
  </div>
</template>

<style scoped>
.ins-finder h3,
.ins-head h3 {
  margin: 0 0 var(--sp-1);
}
.inspect-page > .card {
  margin-bottom: var(--sp-4);
}
.ins-form {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
  flex-wrap: wrap;
}
.ins-form .input {
  flex: 1;
  min-width: 220px;
}
.ins-error {
  color: var(--danger);
  margin: var(--sp-2) 0 0;
}
.ins-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-3);
}
.ins-head p {
  margin: 0;
}
.ins-toggles {
  display: flex;
  gap: var(--sp-4);
  flex-wrap: wrap;
  margin: var(--sp-3) 0;
}
.ins-stage {
  position: relative;
  display: block;
  line-height: 0;
  background: var(--bg-sunken);
  border-radius: var(--radius);
  overflow: hidden;
  /* Vertical swipes stay page scrolls until the reader zooms in. */
  touch-action: pan-y;
  --k: 1;
}
.ins-stage.zoomed {
  touch-action: none;
  cursor: grab;
}
.ins-stage.dragging {
  cursor: grabbing;
}
.ins-layer {
  position: relative;
  transform-origin: 0 0;
  will-change: transform;
  user-select: none;
}
.ins-img {
  display: block;
  width: 100%;
  height: auto;
}
/* Everything drawn over the photo divides by --k, so borders, labels and dots
   keep their on-screen size however far the reader zooms in. */
.ins-box {
  position: absolute;
  padding: 0;
  background: transparent;
  border: max(0.5px, calc(2px / var(--k))) solid var(--action);
  border-radius: 2px;
  cursor: pointer;
}
.ins-box.theirs {
  border-color: var(--warn);
  border-style: dashed;
  cursor: default;
}
.ins-stage.zoomed .ins-box.theirs {
  cursor: inherit;
}
.ins-box.sel {
  border-color: var(--ok);
  box-shadow: 0 0 0 max(0.5px, calc(2px / var(--k))) rgba(31, 157, 77, 0.35);
}
.ins-tag {
  position: absolute;
  left: 0;
  bottom: 100%;
  transform: scale(calc(1 / var(--k)));
  transform-origin: left bottom;
  font-size: var(--fs-sm);
  line-height: 1.4;
  white-space: nowrap;
  padding: 0 4px;
  color: #fff;
  background: var(--action);
  border-radius: 2px 2px 0 0;
}
.ins-box.theirs .ins-tag {
  background: var(--warn);
}
.ins-box.sel .ins-tag {
  background: var(--ok);
}
.ins-point {
  position: absolute;
  width: 5px;
  height: 5px;
  transform: translate(-50%, -50%) scale(calc(1 / var(--k)));
  border-radius: 50%;
  background: var(--ok);
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.8);
}
.ins-zoom {
  position: absolute;
  right: var(--sp-3);
  bottom: var(--sp-3);
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--sp-1);
  line-height: 1;
  z-index: 2;
}
.ins-zoom-level {
  font-size: var(--fs-sm);
  text-align: center;
  padding: 3px 4px;
  color: var(--text-2);
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-menu);
}
.ins-zoom-btn {
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--fs-lg);
  cursor: pointer;
  color: var(--text);
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-menu);
}
.ins-zoom-btn:hover:not(:disabled) {
  background: var(--bg-sunken);
}
.ins-zoom-btn:disabled {
  color: var(--text-3);
  cursor: default;
}
.ins-warn {
  color: var(--warn);
  margin: var(--sp-3) 0 0;
}
.ins-faces {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--sp-3);
  margin-top: var(--sp-3);
}
.ins-face {
  display: flex;
  gap: var(--sp-3);
  padding: var(--sp-3);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  cursor: pointer;
}
.ins-face.sel {
  border-color: var(--ok);
  background: var(--sel-tint);
}
.ins-crop {
  width: 96px;
  height: 96px;
  object-fit: cover;
  border-radius: var(--radius);
  flex: none;
}
.ins-crop-missing {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--fs-sm);
  color: var(--text-3);
  background: var(--bg-sunken);
}
.ins-facts {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 2px var(--sp-3);
  margin: 0;
  font-size: var(--fs-sm);
  min-width: 0;
}
.ins-facts dt {
  color: var(--text-2);
}
.ins-facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.ins-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: var(--sp-3);
  font-size: var(--fs-sm);
}
.ins-table th,
.ins-table td {
  text-align: left;
  padding: var(--sp-1) var(--sp-2);
  border-bottom: 1px solid var(--border-soft);
}
.ins-list {
  margin: var(--sp-2) 0 0;
  padding-left: var(--sp-4);
}
</style>
