<script setup lang="ts">
// Single-job view (/jobs/:id): a JobPanel attached to the route's job plus a
// meta line (name + argv). Ports job.html.j2. The panel handles SSE/polling; the
// meta line is a one-shot GET /api/jobs/{id}.
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import JobPanel from '../components/JobPanel.vue'
import { getJSON } from '../api/client'
import type { Job } from '../api/types'

const route = useRoute()
const id = route.params.id as string
const label = computed(() => (/^\d+$/.test(id) ? '#' + id : id))
const meta = ref('')

onMounted(async () => {
  try {
    const m = await getJSON<Job>('/api/jobs/' + id)
    meta.value = m.name + ' · ' + (m.argv || []).join(' ')
  } catch {
    /* unknown job — panel will surface the error */
  }
})
</script>

<template>
  <div class="page">
    <div class="card">
      <h2>Job <span class="mono">{{ label }}</span></h2>
      <p class="muted">{{ meta }}</p>
    </div>
    <JobPanel :job-id="id" />
  </div>
</template>
