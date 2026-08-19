<script setup lang="ts">
// The ONE review-card renderer for all six kinds (assign / low_confidence /
// new_person / reassign / merge / merge_named), collapsing the old duplicated
// pair — the Jinja `review_card` macro and review.js `renderCard()` — into a
// single source of truth. Pure presenter: it renders the shaped item dict from
// review/queries.py and emits `decide` / `name`; the parent owns state, keyboard
// flow, selection and the advance semantics.
//
// Class names + data-* attrs are the contract the CSS (.card / .merge /
// .thumb-group / [data-named-merge] red border …) and the parent's filters rely
// on — kept identical to the legacy markup.
import { computed } from 'vue'
import type { ReviewItem, ReviewPerson, ReviewDecision } from '../../api/types'
import HiddenBadge from './HiddenBadge.vue'

const props = defineProps<{
  item: ReviewItem
  /** Session-local decision; null until acted on. Drives dimming + footer. */
  decision?: ReviewDecision | null
  selected?: boolean
}>()

const emit = defineEmits<{
  (e: 'decide', decision: ReviewDecision): void
  (e: 'name', value: string): void
  /** Open the person picker: 'merge' folds a new person into an existing one,
   * 'reassign' re-points a single face. */
  (e: 'retarget', mode: 'merge' | 'reassign'): void
  (e: 'select'): void
}>()

const STATUS_BY_DECISION: Record<ReviewDecision, string> = {
  approve: 'approved',
  reject: 'rejected',
  hide: 'hidden',
}

const decided = computed(() => props.decision != null)
// The footer's status half is the client-side status display, matching
// review.js setCardStatus(): the decision wins over the server-loaded status.
const displayStatus = computed(() =>
  props.decision ? STATUS_BY_DECISION[props.decision] : props.item.status,
)

const p = computed(() => props.item.payload)

function personName(person?: ReviewPerson | null): string {
  if (!person) return ''
  return String(person.name || person.person_id || '')
}

function fmt(n: number | null | undefined): string {
  return (n ?? 0).toFixed(3)
}

function onName(e: Event): void {
  emit('name', (e.target as HTMLInputElement).value)
}

// Click anywhere on the card to make it the current one, so the hotkeys act on
// what was clicked. The card's own controls keep their meaning.
function onActivate(e: Event): void {
  if ((e.target as HTMLElement).closest('button, a, input')) return
  emit('select')
}
</script>

<template>
  <div
    class="card"
    :class="{ sel: selected, decided }"
    :data-id="item.item_id"
    :data-unnamed-target="item.unnamed_target ? '1' : undefined"
    :data-unnamed-merge="item.unnamed_merge ? '1' : undefined"
    :data-named-merge="item.named_merge ? '1' : undefined"
    tabindex="0"
    @click="onActivate"
    @focusin="onActivate"
  >
    <!-- Top face crop for every non-reassign kind. -->
    <template v-if="item.crop && item.kind !== 'reassign'">
      <a
        v-if="item.item_url"
        :href="item.item_url"
        target="_blank"
        rel="noopener"
        title="open photo in Synology Photos"
        ><img :src="item.crop" alt="face crop" loading="lazy" decoding="async"
      /></a>
      <img v-else :src="item.crop" alt="face crop" loading="lazy" decoding="async" />
    </template>

    <!-- new_person: exemplar thumbs + editable suggested name. -->
    <template v-if="item.kind === 'new_person'">
      <div class="thumbs">
        <template v-for="(c, i) in item.new_person_crops" :key="i">
          <img v-if="c" :src="c" alt="" loading="lazy" decoding="async" />
        </template>
      </div>
      <input
        class="name-input"
        placeholder="suggested name"
        :value="p.suggested_name || ''"
        @change="onName"
        aria-label="Suggested name"
      />
    </template>

    <!-- merge / merge_named: two named sides with exemplar thumbs. -->
    <template v-else-if="item.kind === 'merge' || item.kind === 'merge_named'">
      <div
        v-if="item.named_merge"
        class="danger-banner"
        title="Both people are already named — merging is irreversible and destroys a human label"
      >
        &#9888; named &harr; named — irreversible, destroys a human label
      </div>
      <div class="merge">
        <div class="merge-side">
          <div class="merge-name">
            <strong>
              <a
                v-if="item.person_a_url"
                :href="item.person_a_url"
                target="_blank"
                rel="noopener"
                >{{ personName(p.person_a) }}</a
              >
              <template v-else>{{ personName(p.person_a) }}</template>
            </strong>
          </div>
          <div class="thumb-group">
            <HiddenBadge v-if="item.person_a_hidden" />
            <div class="thumbs">
              <template v-for="(c, i) in item.merge_crops_a" :key="i">
                <img v-if="c" :src="c" alt="" loading="lazy" decoding="async" />
              </template>
            </div>
          </div>
        </div>
        <div class="merge-arrow" aria-hidden="true">&harr;</div>
        <div class="merge-side">
          <div class="merge-name">
            <strong>
              <a
                v-if="item.person_b_url"
                :href="item.person_b_url"
                target="_blank"
                rel="noopener"
                >{{ personName(p.person_b) }}</a
              >
              <template v-else>{{ personName(p.person_b) }}</template>
            </strong>
          </div>
          <div class="thumb-group">
            <HiddenBadge v-if="item.person_b_hidden" />
            <div class="thumbs">
              <template v-for="(c, i) in item.merge_crops_b" :key="i">
                <img v-if="c" :src="c" alt="" loading="lazy" decoding="async" />
              </template>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- reassign: from-person (with the face crop) → target person. -->
    <template v-else-if="item.kind === 'reassign'">
      <div class="merge">
        <div class="merge-side">
          <div class="merge-name">
            <strong>
              <a
                v-if="item.from_person_url"
                :href="item.from_person_url"
                target="_blank"
                rel="noopener"
                >{{ p.from_person_name || p.from_person_id }}</a
              >
              <template v-else>{{ p.from_person_name || p.from_person_id }}</template>
            </strong>
          </div>
          <template v-if="item.crop">
            <a
              v-if="item.item_url"
              :href="item.item_url"
              target="_blank"
              rel="noopener"
              title="open photo in Synology Photos"
              ><img :src="item.crop" alt="face crop" loading="lazy" decoding="async"
            /></a>
            <img
              v-else
              :src="item.crop"
              alt="face crop"
              loading="lazy"
              decoding="async"
            />
          </template>
        </div>
        <div class="merge-arrow" aria-hidden="true">&rarr;</div>
        <div class="merge-side">
          <div class="merge-name">
            <strong>
              <a
                v-if="item.person_url"
                :href="item.person_url"
                target="_blank"
                rel="noopener"
                >{{ p.person_name || p.person_id }}</a
              >
              <template v-else>{{ p.person_name || p.person_id }}</template>
            </strong>
          </div>
          <div class="thumb-group">
            <HiddenBadge v-if="item.target_hidden" />
            <div class="thumbs">
              <img
                v-for="(c, i) in item.target_crops"
                :key="i"
                :src="c"
                alt=""
                loading="lazy"
                decoding="async"
              />
            </div>
          </div>
        </div>
      </div>
      <div class="muted">
        to-sim {{ fmt(item.confidence) }}<template
          v-if="p.from_similarity != null"
        >
          · from-sim {{ fmt(p.from_similarity) }}</template
        >
      </div>
    </template>

    <!-- assign / low_confidence: a single target person + confidence. -->
    <template v-else>
      <div class="merge-name">
        <strong>{{ p.person_name || p.person_id }}</strong
        ><HiddenBadge v-if="item.target_hidden" cls="name-hidden" />
      </div>
      <div v-if="p.manual_target" class="thumbs">
        <img
          v-for="(c, i) in item.target_crops"
          :key="i"
          :src="c"
          alt=""
          loading="lazy"
          decoding="async"
        />
      </div>
      <!-- A retargeted item's stored confidence was a similarity to the person
           the human overruled, so there is no score left to show. -->
      <div class="muted">
        <template v-if="p.manual_target">you picked this person</template>
        <template v-else>conf {{ fmt(item.confidence) }}</template>
      </div>
    </template>

    <div class="muted card-kind">{{ item.kind }} · {{ displayStatus }}</div>
    <div class="card-actions">
      <button
        class="btn btn-sm btn-primary"
        type="button"
        @click="emit('decide', 'approve')"
      >
        &check; <kbd>y</kbd>
      </button>
      <button
        class="btn btn-sm btn-ghost"
        type="button"
        @click="emit('decide', 'reject')"
      >
        &cross; <kbd>n</kbd>
      </button>
      <template v-if="item.kind === 'new_person'">
        <button
          class="btn btn-sm btn-ghost"
          type="button"
          title="Never suggest this group again"
          @click="emit('decide', 'hide')"
        >
          Hide <kbd>h</kbd>
        </button>
        <button
          class="btn btn-sm btn-ghost"
          type="button"
          title="These faces belong to a person who already exists"
          @click="emit('retarget', 'merge')"
        >
          Merge into&hellip; <kbd>m</kbd>
        </button>
      </template>
      <button
        v-else-if="item.kind === 'assign' || item.kind === 'low_confidence'"
        class="btn btn-sm btn-ghost"
        type="button"
        title="Pick a different person for this face"
        @click="emit('retarget', 'reassign')"
      >
        Reassign&hellip; <kbd>r</kbd>
      </button>
    </div>
  </div>
</template>

<style scoped>
.card {
  padding: var(--sp-3);
  font-size: var(--fs-base);
}
.card.sel {
  outline: 3px solid var(--action);
  outline-offset: 1px;
}
.card.decided {
  opacity: 0.35;
}
.card[data-named-merge] {
  border-color: var(--danger);
  box-shadow: 0 0 0 1px var(--danger) inset;
}
.card img {
  width: 96px;
  height: 96px;
  object-fit: cover;
  border-radius: var(--radius);
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
.thumb-group > :deep(.hidden-badge) {
  position: absolute;
  top: 3px;
  right: 3px;
  z-index: 1;
}
.name-input {
  width: 100%;
  margin-top: var(--sp-2);
  font: inherit;
  padding: 4px var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-raised);
  color: var(--text);
}
.danger-banner {
  background: var(--danger);
  color: #fff;
  font-weight: 600;
  font-size: var(--fs-sm);
  padding: var(--sp-1) var(--sp-2);
  border-radius: var(--radius);
  margin-bottom: var(--sp-2);
}
.card-kind {
  margin-top: var(--sp-1);
}
.card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
}
/* new_person carries four actions, which do not fit one row of a 240px card:
   let a whole button drop to the next row rather than squeeze its label. */
.card-actions .btn {
  white-space: nowrap;
}
</style>
