<script setup lang="ts">
// Bulk-approve control (the toolbar's `.bulk` cluster): approve every pending
// item of the current kind at/above a minimum confidence. The legacy UI fired
// this with no confirmation and a full page reload; per the plan it now gates on
// a confirm() first, then emits `approve` for the parent to POST + reload.
// The effective kind mirrors review.js: the current filter kind, or "assign"
// when the filter is "all kinds".
import { ref, computed } from 'vue'
import { confirm } from '../../composables/useConfirm'

const props = defineProps<{ kind: string }>()

const emit = defineEmits<{
  (e: 'approve', payload: { kind: string; minConfidence: number }): void
}>()

const minConf = ref(0.55)

const effectiveKind = computed(() => props.kind || 'assign')

async function submit(): Promise<void> {
  const ok = await confirm({
    title: 'Bulk approve',
    message: `Approve all pending ${effectiveKind.value} items with confidence ≥ ${minConf.value}?`,
    okLabel: 'Approve',
    danger: false,
  })
  if (!ok) return
  emit('approve', { kind: effectiveKind.value, minConfidence: minConf.value })
}
</script>

<template>
  <div class="bulk">
    <input
      v-model.number="minConf"
      class="input input-sm"
      type="number"
      step="0.01"
      aria-label="Minimum confidence for bulk approve"
    />
    <button class="btn btn-sm" type="button" @click="submit">
      Approve all {{ effectiveKind }} &ge; conf
    </button>
  </div>
</template>
