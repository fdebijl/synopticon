<script setup lang="ts">
// Phase 0 stub — replaced with the live job view (SSE stream) in Phase 1.
import { useRoute } from 'vue-router'

const route = useRoute()
const id = route.params.id as string
</script>

<template>
  <div class="page">
    <h2>Job {{ id }}</h2>
    <p class="muted">Coming soon.</p>
  </div>
</template>
