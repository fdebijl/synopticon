<script setup lang="ts">
// QuickMerger: triage the backlog of unnamed Synology people, one card at a
// time. Writes hit the NAS directly (not the review queue), so the first write of a
// session asks for confirmation once; after that the flow stays uninterrupted.
// The server enforces its own gates regardless: `confirm: true` on every write
// and a hard refusal to merge a person that has a name on the NAS.
import { ref, computed, onBeforeUnmount, nextTick } from 'vue'
import { getJSON, postJSON, ApiError } from '../api/client'
import { toast } from '../stores/toasts'
import { confirm } from '../composables/useConfirm'

interface QmPerson {
  id: number
  space: string
  name: string
  item_count: number | null
  thumb_url: string | null
  link: string | null
}
interface Status {
  spaces: string[]
  nas_configured: boolean
  web_base: string | null
}

// The HIDE_CODE is quickly typeable to prevent interrupting the flow of merging faces quickly
const HIDE_CODE = '11'
const SUGGEST_DEBOUNCE = 300

const status = ref<Status | null>(null)
const space = ref('')
const people = ref<QmPerson[]>([])
const index = ref(0)
const loading = ref(false)
const busy = ref(false)
const loaded = ref(false)

const query = ref('')
const suggestions = ref<QmPerson[]>([])
const selected = ref(-1)
const searching = ref(false)
const inputEl = ref<HTMLInputElement | null>(null)

let armed = false
let debounceTimer: number | null = null
let suggestSeq = 0

const current = computed<QmPerson | null>(() => people.value[index.value] ?? null)
const target = computed<QmPerson | null>(() =>
  selected.value >= 0 ? (suggestions.value[selected.value] ?? null) : null,
)
const done = computed(() => loaded.value && index.value >= people.value.length)

async function loadStatus(): Promise<void> {
  try {
    status.value = await getJSON<Status>('/api/quickmerger/status')
    if (!space.value) space.value = status.value.spaces[0] ?? 'personal'
  } catch (e) {
    toast((e as Error).message || 'Failed to read QuickMerger status', 'error')
  }
}

async function load(refresh = false): Promise<void> {
  if (!space.value) await loadStatus()
  loading.value = true
  try {
    const data = await getJSON<{ persons: QmPerson[] }>(
      `/api/quickmerger/persons?space=${encodeURIComponent(space.value)}` +
        (refresh ? '&refresh=true' : ''),
    )
    people.value = data.persons
    index.value = 0
    loaded.value = true
    resetInput()
    await nextTick()
    inputEl.value?.focus()
  } catch (e) {
    toast((e as Error).message || 'Failed to load people', 'error')
  } finally {
    loading.value = false
  }
}

function resetInput(): void {
  query.value = ''
  suggestions.value = []
  selected.value = -1
  searching.value = false
  if (debounceTimer !== null) window.clearTimeout(debounceTimer)
  debounceTimer = null
}

function advance(by = 1): void {
  index.value = Math.min(index.value + by, people.value.length)
  resetInput()
  void nextTick(() => inputEl.value?.focus())
}

function back(): void {
  if (index.value === 0) return
  index.value--
  resetInput()
  void nextTick(() => inputEl.value?.focus())
}

/** One confirmation per session before the first NAS write. */
async function arm(): Promise<boolean> {
  if (armed) return true
  const ok = await confirm({
    title: 'QuickMerger writes to the NAS',
    message:
      'Naming and hiding are reversible. Merging an unnamed person into an ' +
      'existing one is not: the merged person is gone from Synology Photos. ' +
      'You will not be asked again this session.',
    okLabel: 'I understand',
  })
  armed = ok
  return ok
}

function onInput(): void {
  const value = query.value.trim()
  // The userscript's muscle-memory shortcut: typing 11 hides, no Enter needed.
  if (value === HIDE_CODE && selected.value === -1) {
    void hide()
    return
  }
  if (debounceTimer !== null) window.clearTimeout(debounceTimer)
  if (!value) {
    suggestions.value = []
    selected.value = -1
    searching.value = false
    return
  }
  searching.value = true
  debounceTimer = window.setTimeout(() => void search(value), SUGGEST_DEBOUNCE)
}

async function search(prefix: string): Promise<void> {
  const seq = ++suggestSeq
  try {
    const data = await getJSON<{ suggestions: QmPerson[] }>(
      `/api/quickmerger/suggest?space=${encodeURIComponent(space.value)}` +
        `&prefix=${encodeURIComponent(prefix)}`,
    )
    if (seq !== suggestSeq) return // a newer keystroke already won
    suggestions.value = data.suggestions
    selected.value = -1
  } catch (e) {
    if (seq === suggestSeq) {
      suggestions.value = []
      toast((e as Error).message || 'Suggestion lookup failed', 'error')
    }
  } finally {
    if (seq === suggestSeq) searching.value = false
  }
}

function move(delta: number): void {
  if (!suggestions.value.length) return
  const n = suggestions.value.length
  selected.value = (selected.value + delta + n) % n
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    move(1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    move(-1)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    void submit()
  }
}

async function write<T>(url: string, body: Record<string, unknown>): Promise<T | null> {
  busy.value = true
  try {
    return await postJSON<T>(url, { ...body, confirm: true })
  } catch (e) {
    const msg =
      e instanceof ApiError && typeof e.body?.error === 'string'
        ? e.body.error
        : (e as Error).message || 'Write failed'
    toast(msg, 'error')
    return null
  } finally {
    busy.value = false
  }
}

async function submit(): Promise<void> {
  const person = current.value
  if (!person || busy.value) return
  const value = query.value.trim()

  if (value === HIDE_CODE && selected.value === -1) return void hide()
  if (!value && selected.value === -1) return advance()
  if (!(await arm())) return

  if (selected.value !== -1) {
    const into = suggestions.value[selected.value]
    if (!into) return
    const res = await write('/api/quickmerger/merge', {
      space: space.value,
      source_id: person.id,
      target_id: into.id,
    })
    if (res === null) return
    toast(`Merged #${person.id} into ${into.name}`, 'ok')
  } else {
    const res = await write('/api/quickmerger/name', {
      space: space.value,
      person_id: person.id,
      name: value,
    })
    if (res === null) return
    toast(`Named #${person.id} ${value}`, 'ok')
  }
  advance()
}

async function hide(): Promise<void> {
  const person = current.value
  if (!person || busy.value) return
  if (!(await arm())) return
  const res = await write('/api/quickmerger/hide', {
    space: space.value,
    person_id: person.id,
  })
  if (res === null) return
  toast(`Hid #${person.id}`, 'ok')
  advance()
}

function pick(i: number): void {
  selected.value = i
  void submit()
}

onBeforeUnmount(() => {
  if (debounceTimer !== null) window.clearTimeout(debounceTimer)
})

void loadStatus()
</script>

<template>
  <section class="card qm">
    <header class="qm-head">
      <div>
        <h3>QuickMerger</h3>
        <p class="muted">
          Quickly work through people Synology has not named yet: type a name to set it, pick a suggestion to merge into that person, or hide the ones that are not people at all.
        </p>
      </div>
      <div class="qm-head-actions">
        <label v-if="(status?.spaces.length ?? 0) > 1" class="qm-space">
          Space
          <select class="select" v-model="space" :disabled="loading" @change="loaded = false">
            <option v-for="s in status?.spaces" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <button type="button" class="btn" :disabled="loading" @click="load(loaded)">
          {{ loading ? 'Loading…' : loaded ? 'Reload' : 'Load unnamed people' }}
        </button>
      </div>
    </header>

    <p v-if="status && !status.nas_configured" class="muted">
      No NAS credentials configured — set them in Settings first.
    </p>

    <p v-else-if="!loaded && !loading" class="muted">
      Nothing loaded yet, click 'Load unnamed people' to get started. 
      The QuickMerger uses a different way to get the faces than the sync job, so you can run this anytime, regardless of the sync status.
    </p>

    <p v-else-if="loading" class="muted">Fetching people from the NAS…</p>

    <p v-else-if="!people.length" class="muted">No unnamed people in this space - nothing to do :D</p>

    <p v-else-if="done" class="muted">
      Processing complete — {{ people.length }} people handled or skipped.
      <button type="button" class="btn btn-sm" @click="load(true)">Reload list</button>
    </p>

    <template v-else-if="current">
      <div class="qm-thumbs">
        <figure class="qm-fig">
          <img v-if="current.thumb_url" class="qm-thumb" :src="current.thumb_url" alt="" />
          <div v-else class="qm-thumb qm-thumb-empty">no thumbnail</div>
          <figcaption class="muted">
            #{{ current.id }}
            <template v-if="current.item_count != null"> · {{ current.item_count }} photos</template>
          </figcaption>
        </figure>
        <div class="qm-arrow" aria-hidden="true">→</div>
        <figure class="qm-fig">
          <img v-if="target?.thumb_url" class="qm-thumb" :src="target.thumb_url" alt="" />
          <div v-else class="qm-thumb qm-thumb-empty">Start typing a name to see their preview.</div>
          <figcaption class="muted">{{ target ? target.name : '—' }}</figcaption>
        </figure>
      </div>

      <p class="qm-progress">
        Person {{ index + 1 }} / {{ people.length }}
        <a v-if="current.link" :href="current.link" target="_blank" rel="noreferrer noopener"
          >View in Synology Photos</a
        >
      </p>

      <input
        ref="inputEl"
        class="input qm-input"
        type="text"
        autocomplete="off"
        :disabled="busy"
        placeholder="Name to set, or search a person to merge into (11 hides)"
        v-model="query"
        @input="onInput"
        @keydown="onKeydown"
      />

      <ul v-if="searching || suggestions.length" class="qm-suggestions">
        <li v-if="searching" class="muted">Searching…</li>
        <li
          v-for="(s, i) in suggestions"
          :key="s.id"
          :class="{ selected: i === selected }"
          @click="pick(i)"
        >
          {{ s.name }} <span class="muted">(#{{ s.id }})</span>
        </li>
      </ul>

      <div class="qm-actions">
        <button type="button" class="btn" :disabled="index === 0 || busy" @click="back">
          Previous
        </button>
        <button type="button" class="btn" :disabled="busy" @click="advance()">Skip</button>
        <button type="button" class="btn" :disabled="busy" @click="advance(10)">Skip 10</button>
        <button type="button" class="btn" :disabled="busy" @click="advance(100)">Skip 100</button>
        <button type="button" class="btn btn-danger" :disabled="busy" @click="hide">Hide</button>
      </div>

      <p class="muted">
        Enter applies · ↑/↓ pick a merge target · empty Enter skips · typing 11 hides.
      </p>
    </template>
  </section>
</template>

<style scoped>
.qm {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.qm h3 {
  margin: 0;
}
.qm-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--sp-3);
  flex-wrap: wrap;
}
.qm-head p {
  margin: var(--sp-1) 0 0;
  max-width: 100ch;
}
.qm-head-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.qm-space {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-sm);
}
.qm-thumbs {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}
.qm-fig {
  margin: 0;
  flex: 0 0 auto;
  text-align: center;
}
.qm-thumb {
  width: 180px;
  height: 180px;
  object-fit: cover;
  border-radius: var(--radius);
  background: var(--bg-sunken);
}
.qm-thumb-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--fs-sm);
  color: var(--text-2);
  padding: 5px;
  border: 1px dashed var(--border-soft);
}
.qm-arrow {
  font-size: 1.5rem;
  color: var(--text-2);
}
.qm-progress {
  display: flex;
  gap: var(--sp-2);
  align-items: baseline;
  margin: 0;
}
.qm-input {
  width: 100%;
}
.qm-suggestions {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  max-height: 180px;
  overflow-y: auto;
}
.qm-suggestions li {
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
}
.qm-suggestions li.selected,
.qm-suggestions li:hover {
  background: var(--sel-tint);
}
.qm-actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
</style>
