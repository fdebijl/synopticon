<script setup lang="ts">
// Focus layout: one big current card + a horizontal carousel of thumbs, one per
// rendered item. Replaces the legacy DOM-projection hack — both the big card and
// the carousel are plain projections of the parent's reactive items array (no
// deep-cloning, no manual value mirroring). The current thumb is scrolled to
// centre; clicking a thumb selects it. Prev/next navigation is the ←/→ keyboard
// flow owned by the parent; page prefetch near the tail is also driven by the
// parent (it owns the full loaded list), since the grid's IntersectionObserver
// sentinel is not mounted here.
import { ref, computed, watch, nextTick } from 'vue'
import type {
  ClientReviewItem,
  ReviewPerson,
  ReviewDecision,
} from '../../api/types'
import ReviewCard from './ReviewCard.vue'
import ReviewCardSkeleton from './ReviewCardSkeleton.vue'

const props = defineProps<{
  items: ClientReviewItem[]
  currentId: number | null
  emptyMessage: string
  loading: boolean
}>()

// Initial-load skeleton: nothing loaded yet and a fetch is in flight.
const showSkeleton = computed(() => props.loading && !props.items.length)

const emit = defineEmits<{
  (e: 'decide', payload: { item: ClientReviewItem; decision: ReviewDecision }): void
  (e: 'name', payload: { item: ClientReviewItem; value: string }): void
  (e: 'retarget', payload: { item: ClientReviewItem; mode: 'merge' | 'reassign' }): void
  (e: 'select', item: ClientReviewItem): void
}>()

const carouselEl = ref<HTMLElement | null>(null)

const current = computed<ClientReviewItem | null>(
  () => props.items.find((it) => it.item_id === props.currentId) ?? null,
)

const KIND_ABBREV: Record<string, string> = {
  assign: 'asn',
  low_confidence: 'low',
  reassign: 'rea',
  merge: 'mrg',
  merge_named: 'm·n',
  new_person: 'new',
}
function kindAbbrev(kind: string): string {
  return KIND_ABBREV[kind] || (kind || '?').slice(0, 3)
}

// First crop shown in the grid card, in DOM order, else null (kind abbreviation).
function thumbSrc(it: ClientReviewItem): string | null {
  if (it.crop) return it.crop
  const pools = [it.new_person_crops, it.merge_crops_a, it.merge_crops_b, it.target_crops]
  for (const pool of pools) {
    for (const c of pool ?? []) if (c) return c
  }
  return null
}

function personName(person?: ReviewPerson | null): string {
  if (!person) return ''
  return String(person.name || person.person_id || '')
}

function names(it: ClientReviewItem): string[] {
  const p = it.payload
  if (it.kind === 'merge' || it.kind === 'merge_named') {
    return [personName(p.person_a), personName(p.person_b)].filter(Boolean)
  }
  if (it.kind === 'reassign') {
    return [
      String(p.from_person_name || p.from_person_id || ''),
      String(p.person_name || p.person_id || ''),
    ].filter(Boolean)
  }
  if (it.kind === 'new_person') return []
  return [String(p.person_name || p.person_id || '')].filter(Boolean)
}

function statusOf(it: ClientReviewItem): string {
  if (it.decision === 'approve') return 'approved'
  if (it.decision === 'reject') return 'rejected'
  if (it.decision === 'hide') return 'hidden'
  return it.status || 'pending'
}

// data-decision drives the check/cross/slash overlay.
function decisionOf(it: ClientReviewItem): string | undefined {
  if (it.decision === 'approve') return 'approved'
  if (it.decision === 'reject') return 'rejected'
  if (it.decision === 'hide') return 'hidden'
  return undefined
}

function thumbAria(it: ClientReviewItem): string {
  let label = `${it.kind}, ${statusOf(it)}`
  const n = names(it)
  if (n.length) label += ` — ${n.join(' ↔ ')}`
  return label
}

const reducedMotion =
  typeof window !== 'undefined' &&
  window.matchMedia &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

function scrollCurrentIntoView(): void {
  if (props.currentId == null || !carouselEl.value) return
  const el = carouselEl.value.querySelector<HTMLElement>(
    `[data-id="${props.currentId}"]`,
  )
  el?.scrollIntoView({
    inline: 'center',
    block: 'nearest',
    behavior: reducedMotion ? 'auto' : 'smooth',
  })
}

watch(
  () => props.currentId,
  () => {
    void nextTick(scrollCurrentIntoView)
  },
  { immediate: true },
)
</script>

<template>
  <div class="focus-view" id="focus-view">
    <div
      class="focus-current"
      id="focus-current"
      aria-live="polite"
      :aria-busy="showSkeleton || undefined"
    >
      <ReviewCard
        v-if="current"
        :item="current"
        :decision="current.decision"
        @decide="(d) => emit('decide', { item: current!, decision: d })"
        @name="(v) => emit('name', { item: current!, value: v })"
        @retarget="(m) => emit('retarget', { item: current!, mode: m })"
      />
      <ReviewCardSkeleton v-else-if="showSkeleton" />
    </div>
    <p v-if="!current && !showSkeleton" class="muted focus-empty">{{ emptyMessage }}</p>
    <div class="carousel" ref="carouselEl" aria-label="Review queue">
      <template v-if="showSkeleton">
        <div
          v-for="n in 12"
          :key="`sk-${n}`"
          class="carousel-thumb skeleton"
          aria-hidden="true"
        ></div>
      </template>
      <button
        v-for="it in items"
        :key="it.item_id"
        type="button"
        class="carousel-thumb"
        :class="{ 'no-img': !thumbSrc(it), decided: !!it.decision }"
        :data-id="it.item_id"
        :data-decision="decisionOf(it)"
        :data-unnamed-target="it.unnamed_target ? '1' : undefined"
        :data-unnamed-merge="it.unnamed_merge ? '1' : undefined"
        :data-named-merge="it.named_merge ? '1' : undefined"
        :aria-current="it.item_id === currentId ? 'true' : undefined"
        :aria-label="thumbAria(it)"
        @click="emit('select', it)"
      >
        <img
          v-if="thumbSrc(it)"
          :src="thumbSrc(it)!"
          alt=""
          loading="lazy"
          decoding="async"
        />
        <template v-else>{{ kindAbbrev(it.kind) }}</template>
      </button>
    </div>
  </div>
</template>

<style scoped>
/* Big card: the grid .card scaled up for a single-item focus. */
.focus-current {
  display: flex;
  justify-content: center;
  margin-bottom: var(--sp-4);
}
.focus-current :deep(.card) {
  max-width: 720px;
  width: 100%;
  padding: var(--sp-5);
  font-size: var(--fs-lg);
}
.focus-current :deep(.card.sel) {
  outline: none;
}
.focus-current :deep(.card img) {
  width: 224px;
  height: 224px;
}
.focus-current :deep(.card .thumbs img) {
  width: 88px;
  height: 88px;
}
.focus-current :deep(.card .card-actions .btn) {
  font-size: var(--fs-lg);
  padding: var(--sp-2) var(--sp-4);
}
.focus-current :deep(.skeleton-img) {
  width: 224px;
  height: 224px;
}
.focus-empty {
  text-align: center;
  padding: var(--sp-5);
}

/* Carousel: horizontal strip of thumbs, current one centered. */
.carousel {
  display: flex;
  gap: var(--sp-2);
  overflow-x: auto;
  padding: var(--sp-2);
  background: var(--bg-sunken);
  border-radius: var(--radius-lg);
  scroll-behavior: smooth;
}
.carousel-thumb {
  flex: 0 0 auto;
  width: 64px;
  height: 64px;
  padding: 0;
  border: 2px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-raised);
  cursor: pointer;
  position: relative;
  overflow: hidden;
  color: var(--text-2);
}
.carousel-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  border-radius: 0;
}
.carousel-thumb.no-img {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--fs-sm);
  text-align: center;
}
.carousel-thumb[aria-current='true'] {
  border-color: var(--action);
  box-shadow: 0 0 0 2px var(--action);
}
.carousel-thumb.decided {
  opacity: 0.45;
}
.carousel-thumb.decided::after {
  position: absolute;
  top: 2px;
  right: 4px;
  font-size: var(--fs-base);
  font-weight: 700;
}
.carousel-thumb[data-decision='approved']::after {
  content: '\2713';
  color: var(--ok);
}
.carousel-thumb[data-decision='rejected']::after {
  content: '\2717';
  color: var(--danger);
}
.carousel-thumb[data-decision='hidden']::after {
  content: '\2298';
  color: var(--text-2);
}
.carousel-thumb.skeleton {
  border-color: var(--border-soft);
}

@media (max-width: 900px) {
  .focus-current :deep(.card) {
    padding: var(--sp-3);
  }
  .focus-current :deep(.card img) {
    width: 160px;
    height: 160px;
  }
  .focus-current :deep(.skeleton-img) {
    width: 160px;
    height: 160px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .carousel {
    scroll-behavior: auto;
  }
}
</style>
