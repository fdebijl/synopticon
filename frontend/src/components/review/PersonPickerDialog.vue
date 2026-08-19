<script setup lang="ts">
// Pick the person a suggestion should have pointed at: "merge into" for a new
// person, "reassign" for an assign the pipeline aimed at the wrong face. Both
// only rewrite the review queue — the NAS write still happens later through
// Apply, under its own flags.
//
// A native <dialog> is load-bearing, not cosmetic: useKeyboard's guard ignores
// page shortcuts while `dialog[open]` matches, so y/n/j/k cannot fire on the
// card behind this one.
//
// The search box mirrors QuickMerger's (debounce + sequence guard + ↑/↓/Enter)
// but deliberately does not share it: this one searches the local persons
// mirror so review keeps working with no NAS reachable, it offers named people
// only, and QuickMerger's input carries its own "11 hides" shortcut.
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import { getJSON } from '../../api/client'
import { toast } from '../../stores/toasts'
import type { ClientReviewItem, ReviewPersonSuggestion } from '../../api/types'
import HiddenBadge from './HiddenBadge.vue'

const SUGGEST_DEBOUNCE = 300

const props = defineProps<{
  item: ClientReviewItem
  mode: 'merge' | 'reassign'
  busy?: boolean
}>()

const emit = defineEmits<{
  (e: 'confirm', target: ReviewPersonSuggestion): void
  (e: 'cancel'): void
}>()

const dlg = ref<HTMLDialogElement | null>(null)
const inputEl = ref<HTMLInputElement | null>(null)
const query = ref('')
const suggestions = ref<ReviewPersonSuggestion[]>([])
const selected = ref(-1)
const searching = ref(false)

let debounceTimer: number | null = null
let suggestSeq = 0

const p = computed(() => props.item.payload)
const target = computed<ReviewPersonSuggestion | null>(() =>
  selected.value >= 0 ? (suggestions.value[selected.value] ?? null) : null,
)

const title = computed(() =>
  props.mode === 'merge' ? 'Merge these faces into…' : 'Reassign this face to…',
)
// A new_person item has no space of its own — the target's space decides which
// of its faces can be tagged. Everything else is pinned to the face's space.
const lockedSpace = computed(() =>
  props.mode === 'merge' ? '' : String(p.value.space || ''),
)
const sourceLabel = computed(() => {
  if (props.mode === 'merge') {
    const n = Number(p.value.size ?? props.item.new_person_crops.length) || 0
    return `${n} face${n === 1 ? '' : 's'}`
  }
  return String(p.value.person_name || p.value.person_id || 'unnamed')
})
const sourceCrops = computed(() =>
  props.mode === 'merge'
    ? props.item.new_person_crops.filter((c): c is string => !!c).slice(0, 6)
    : [props.item.crop].filter((c): c is string => !!c),
)

function open(): void {
  query.value = ''
  suggestions.value = []
  selected.value = -1
  searching.value = false
  requestAnimationFrame(() => {
    if (dlg.value && !dlg.value.open) dlg.value.showModal()
    void nextTick(() => inputEl.value?.focus())
  })
}
open()

watch(
  () => props.item.item_id,
  () => open(),
)

function onInput(): void {
  const value = query.value.trim()
  if (debounceTimer !== null) window.clearTimeout(debounceTimer)
  debounceTimer = null
  selected.value = -1
  if (!value) {
    suggestions.value = []
    searching.value = false
    return
  }
  searching.value = true
  debounceTimer = window.setTimeout(() => void search(value), SUGGEST_DEBOUNCE)
}

async function search(q: string): Promise<void> {
  const seq = ++suggestSeq
  try {
    const data = await getJSON<{ persons: ReviewPersonSuggestion[] }>(
      `/api/review/persons?q=${encodeURIComponent(q)}` +
        (lockedSpace.value ? `&space=${encodeURIComponent(lockedSpace.value)}` : ''),
    )
    if (seq !== suggestSeq) return // a newer keystroke already won
    suggestions.value = data.persons
    selected.value = data.persons.length ? 0 : -1
  } catch (e) {
    if (seq === suggestSeq) {
      suggestions.value = []
      toast((e as Error).message || 'Person lookup failed', 'error')
    }
  } finally {
    if (seq === suggestSeq) searching.value = false
  }
}

function move(delta: number): void {
  const n = suggestions.value.length
  if (!n) return
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
    submit()
  }
}

function submit(): void {
  if (props.busy || !target.value) return
  emit('confirm', target.value)
}

function close(): void {
  emit('cancel')
}

onBeforeUnmount(() => {
  if (debounceTimer !== null) window.clearTimeout(debounceTimer)
  if (dlg.value?.open) dlg.value.close()
})
</script>

<template>
  <dialog ref="dlg" class="modal" @cancel.prevent="close" @close="close">
    <div class="modal-body">
      <h3 class="modal-title">{{ title }}</h3>

      <div class="merge pick-preview">
        <div class="merge-side">
          <div class="merge-name">
            <strong>{{ sourceLabel }}</strong>
          </div>
          <div class="thumbs">
            <img
              v-for="(c, i) in sourceCrops"
              :key="i"
              :src="c"
              alt=""
              loading="lazy"
              decoding="async"
            />
          </div>
        </div>
        <div class="merge-arrow" aria-hidden="true">&rarr;</div>
        <div class="merge-side">
          <div class="merge-name">
            <strong v-if="target">{{ target.name }}</strong>
            <span v-else class="muted">pick a person</span>
            <HiddenBadge v-if="target?.hidden" cls="name-hidden" />
          </div>
          <div v-if="target && target.crops.length" class="thumb-group">
            <div class="thumbs">
              <img
                v-for="(c, i) in target.crops"
                :key="i"
                :src="c"
                alt=""
                loading="lazy"
                decoding="async"
              />
            </div>
          </div>
          <div v-else class="pick-empty" aria-hidden="true"></div>
          <div v-if="target" class="muted">
            #{{ target.person_id }}
            <template v-if="target.item_count != null">
              · {{ target.item_count }} photos</template
            >
          </div>
        </div>
      </div>

      <input
        ref="inputEl"
        class="input pick-input"
        type="text"
        autocomplete="off"
        :disabled="busy"
        placeholder="Search people by name"
        aria-label="Search people by name"
        v-model="query"
        @input="onInput"
        @keydown="onKeydown"
      />

      <ul v-if="searching || suggestions.length" class="pick-suggestions">
        <li v-if="searching" class="muted">Searching…</li>
        <li
          v-for="(s, i) in suggestions"
          :key="`${s.space}:${s.person_id}`"
          :class="{ selected: i === selected }"
          @click="selected = i"
          @dblclick="submit"
        >
          {{ s.name }}
          <span class="muted"
            >(#{{ s.person_id }} · {{ s.space }}<template
              v-if="s.item_count != null"
            >
              · {{ s.item_count }} photos</template
            >)</span
          >
        </li>
      </ul>
      <p v-else-if="query.trim()" class="muted">
        No named person matches. Unnamed people are named in Utilities → QuickMerger.
      </p>

      <p class="muted pick-note">
        <template v-if="mode === 'merge'"
          >Queues every face in this group to be tagged as the person you pick. Nothing
          is written to Synology Photos until you run Apply.</template
        >
        <template v-else
          >Replaces the suggested person for this one face. Nothing is written to
          Synology Photos until you run Apply.</template
        >
      </p>

      <div class="modal-actions">
        <button class="btn" type="button" @click="close">Cancel</button>
        <button
          class="btn btn-action"
          type="button"
          :disabled="!target || busy"
          @click="submit"
        >
          {{ mode === 'merge' ? 'Merge into' : 'Reassign to' }}
          {{ target ? target.name : '…' }}
        </button>
      </div>
    </div>
  </dialog>
</template>

<style scoped>
.modal {
  border: none;
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-modal);
  padding: 0;
  max-width: 560px;
  width: 90%;
  background: var(--bg-raised);
  color: var(--text);
}
.modal::backdrop {
  background: rgba(15, 25, 35, 0.4);
}
.modal-body {
  padding: var(--sp-5);
}
.modal-title {
  margin-bottom: var(--sp-3);
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  margin-top: var(--sp-4);
}

/* Same side-by-side grammar as ReviewCard's merge/reassign body. */
.merge {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.merge-side {
  flex: 1;
  min-width: 0;
}
.merge-arrow {
  flex: 0 0 auto;
  opacity: 0.6;
}
.thumb-group {
  position: relative;
  display: inline-block;
}
.thumbs {
  display: flex;
  flex-wrap: wrap;
}
.thumbs img {
  width: 48px;
  height: 48px;
  margin: 2px;
  border-radius: var(--radius);
  object-fit: cover;
}
.pick-preview {
  margin-bottom: var(--sp-3);
  min-height: 96px;
}
.pick-empty {
  width: 48px;
  height: 48px;
  margin: 2px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
}
.pick-input {
  width: 100%;
}
.pick-suggestions {
  list-style: none;
  margin: var(--sp-2) 0 0;
  padding: 0;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  max-height: 180px;
  overflow-y: auto;
}
.pick-suggestions li {
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
}
.pick-suggestions li.selected,
.pick-suggestions li:hover {
  background: var(--sel-tint);
}
.pick-note {
  margin-top: var(--sp-3);
  font-size: var(--fs-sm);
}
</style>
