<script setup lang="ts">
// Models tab: read-only view of the model weights the pipeline needs. Reports
// per-model presence on disk and manifest registration (sha256 recorded) from
// GET /api/models. It does not download or verify — that lives on the Pipeline
// view's "Download models" card (which now also carries the --allow-record-hash
// toggle for manually-copied ONNX files). A hint here points there.
import { onMounted, ref, computed } from 'vue'
import { getJSON } from '../../api/client'
import type { ModelStatus, ModelsResponse } from '../../api/types'

const items = ref<ModelStatus[]>([])
const modelsDir = ref('')
const loaded = ref(false)

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i]
}

function statusLabel(m: ModelStatus): string {
  if (!m.present) return 'missing'
  if (!m.registered) return 'not registered'
  return 'ready'
}

function statusClass(m: ModelStatus): string {
  if (!m.present) return 'st-missing'
  if (!m.registered) return 'st-unregistered'
  return 'st-ready'
}

const readyCount = computed(() => items.value.filter((m) => m.present && m.registered).length)
const anyUnregistered = computed(() => items.value.some((m) => m.present && !m.registered))

async function load(): Promise<void> {
  try {
    const data = await getJSON<ModelsResponse>('/api/models')
    items.value = data.items || []
    modelsDir.value = data.models_dir || ''
  } catch {
    /* transient — leave the current list */
  } finally {
    loaded.value = true
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="settings-panel">
    <div class="card models-section">
      <h3>Model weights</h3>
      <p class="muted">
        Weights the face pipeline needs, in
        <code v-if="modelsDir">{{ modelsDir }}</code><span v-else>the models directory</span>.
        <template v-if="loaded"> {{ readyCount }} / {{ items.length }} ready.</template>
      </p>
      <p v-if="anyUnregistered" class="muted note">
        Some files are present but not registered in the manifest — run
        <strong>Download models</strong> on the Pipeline page with
        <strong>record hash</strong> enabled to register manually-copied weights.
      </p>
      <table class="data models-table">
        <thead>
          <tr>
            <th>Model</th>
            <th>File</th>
            <th>Size</th>
            <th>Status</th>
            <th>sha256</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in items" :key="m.key">
            <td>{{ m.key }}</td>
            <td class="mono">{{ m.file }}</td>
            <td>{{ fmtBytes(m.size) }}</td>
            <td><span class="badge" :class="statusClass(m)">{{ statusLabel(m) }}</span></td>
            <td class="mono sha">{{ m.sha256 ? m.sha256.slice(0, 12) + '…' : '—' }}</td>
          </tr>
          <tr v-if="loaded && !items.length">
            <td colspan="5" class="muted">No model information available.</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.models-section {
  margin-bottom: var(--sp-5);
  max-width: 820px;
}
.models-table {
  width: 100%;
  margin-top: var(--sp-3);
}
.note {
  margin-top: var(--sp-2);
}
.badge.st-ready {
  background: rgba(31, 157, 77, 0.14);
  color: var(--ok);
  border-color: transparent;
}
.badge.st-unregistered {
  background: rgba(224, 133, 15, 0.16);
  color: var(--warn);
  border-color: transparent;
}
.badge.st-missing {
  background: rgba(216, 67, 67, 0.14);
  color: var(--danger);
  border-color: transparent;
}
.sha {
  color: var(--text-2);
}
</style>
