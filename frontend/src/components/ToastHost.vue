<script setup lang="ts">
import { useToasts } from '../stores/toasts'

const { toasts, dismiss } = useToasts()
</script>

<template>
  <div class="toast-region" aria-live="polite" aria-atomic="false">
    <div
      v-for="t in toasts.items"
      :key="t.id"
      class="toast"
      :class="t.kind"
      role="status"
      @click="dismiss(t.id)"
    >
      {{ t.message }}
    </div>
  </div>
</template>
