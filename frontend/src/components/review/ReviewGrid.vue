<script setup lang="ts">
// Grid layout: a responsive card grid over the parent's reactive items array,
// plus the IntersectionObserver sentinel that drives infinite scroll (emits
// `load-more`; the parent guards loading/exhausted). Selection highlight (.sel)
// follows `selectedId`, and the selected card is scrolled into view like the
// legacy review.js select() did (block: "nearest").
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import type { ClientReviewItem, ReviewDecision } from '../../api/types'
import ReviewCard from './ReviewCard.vue'
import ReviewCardSkeleton from './ReviewCardSkeleton.vue'

const props = defineProps<{
  items: ClientReviewItem[]
  selectedId: number | null
  exhausted: boolean
  loading: boolean
}>()

// Skeleton placeholders while a page is in flight: a full grid's worth on the
// initial (empty) load, a small tail when infinite scroll fetches more.
const skeletonCount = computed(() =>
  props.loading ? (props.items.length ? 3 : 8) : 0,
)

const emit = defineEmits<{
  (e: 'decide', payload: { item: ClientReviewItem; decision: ReviewDecision }): void
  (e: 'name', payload: { item: ClientReviewItem; value: string }): void
  (e: 'retarget', payload: { item: ClientReviewItem; mode: 'merge' | 'reassign' }): void
  (e: 'select', item: ClientReviewItem): void
  (e: 'load-more'): void
}>()

const gridEl = ref<HTMLElement | null>(null)
const sentinel = ref<HTMLElement | null>(null)
let io: IntersectionObserver | null = null

function scrollSelectedIntoView(): void {
  if (props.selectedId == null || !gridEl.value) return
  const el = gridEl.value.querySelector<HTMLElement>(
    `[data-id="${props.selectedId}"]`,
  )
  el?.scrollIntoView({ block: 'nearest' })
}

watch(
  () => props.selectedId,
  () => {
    void nextTick(scrollSelectedIntoView)
  },
)

onMounted(() => {
  // Bring the shared selection into view when returning to grid from focus.
  void nextTick(scrollSelectedIntoView)
  if (sentinel.value && 'IntersectionObserver' in window) {
    io = new IntersectionObserver(
      (entries) => {
        if (entries.some((en) => en.isIntersecting)) emit('load-more')
      },
      { rootMargin: '400px' },
    )
    io.observe(sentinel.value)
  }
})

onUnmounted(() => {
  io?.disconnect()
  io = null
})
</script>

<template>
  <div>
    <div class="grid review-grid" ref="gridEl" :aria-busy="loading || undefined">
      <ReviewCard
        v-for="it in items"
        :key="it.item_id"
        :item="it"
        :decision="it.decision"
        :selected="it.item_id === selectedId"
        @decide="(d) => emit('decide', { item: it, decision: d })"
        @name="(v) => emit('name', { item: it, value: v })"
        @retarget="(m) => emit('retarget', { item: it, mode: m })"
        @select="emit('select', it)"
      />
      <ReviewCardSkeleton v-for="n in skeletonCount" :key="`sk-${n}`" />
    </div>
    <div ref="sentinel" class="scroll-sentinel" aria-hidden="true"></div>
    <p v-if="exhausted" class="muted end-note">End of queue.</p>
  </div>
</template>

<style scoped>
.scroll-sentinel {
  height: 1px;
}
.end-note {
  text-align: center;
  padding: var(--sp-4);
}
</style>
