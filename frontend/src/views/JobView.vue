<script setup lang="ts">
// Single-job view (/jobs/:id): a JobPanel attached to the route's job. The panel
// owns the transport *and* the metadata fetch (name/argv/timings), so this view
// stays a thin heading over it — fetching the same /api/jobs/{id} here too would
// just double the request on every page open.
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import JobPanel from '../components/JobPanel.vue'

const route = useRoute()
const id = route.params.id as string
const label = computed(() => (/^\d+$/.test(id) ? '#' + id : id))
</script>

<template>
  <div class="page">
    <div class="card">
      <h2>Job <span class="mono">{{ label }}</span></h2>
    </div>
    <JobPanel :job-id="id" />
  </div>
</template>
