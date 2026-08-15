<script setup lang="ts">
// About page: what this build is, and the environment facts a bug report needs.
// Everything comes from GET /api/about (import-light on the server side) plus
// the cached /api/auth/me version so the header renders before the fetch lands.
import { ref, computed, onMounted } from 'vue'
import { getJSON } from '../api/client'
import type { AboutInfo } from '../api/types'
import { useAuth } from '../stores/auth'

const REPO = 'https://github.com/fdebijl/synopticon'

const info = ref<AboutInfo | null>(null)
const loaded = ref(false)
const copied = ref(false)
const { state: auth } = useAuth()

const version = computed(() => info.value?.version ?? auth.me?.version ?? '')
const repo = computed(() => info.value?.repo_url || REPO)

const packages = computed(() =>
  Object.entries(info.value?.packages ?? {}).filter(([, v]) => v !== null),
)

interface Row {
  label: string
  value: string
  mono?: boolean
}

const rows = computed<Row[]>(() => {
  const i = info.value
  if (!i) return []
  const cores =
    `${i.cpu.available_cores} usable · ${i.cpu.physical_cores} physical` +
    (i.cpu.cgroup_quota != null ? ` · cgroup quota ${i.cpu.cgroup_quota}` : '')
  return [
    { label: 'Version', value: 'v' + i.version },
    {
      label: 'Pipeline version',
      value: i.pipeline_version ?? (i.models_ready ? 'unknown' : 'model weights missing'),
      mono: i.pipeline_version != null,
    },
    { label: 'Python', value: i.python },
    { label: 'Platform', value: i.platform },
    { label: 'CPU', value: cores },
    { label: 'Data directory', value: i.paths.data_dir, mono: true },
    { label: 'Models directory', value: i.paths.models_dir, mono: true },
    { label: 'Database', value: i.paths.db_path, mono: true },
  ]
})

// One paste-ready block for GitHub issues — the whole reason the environment
// facts are on a page instead of only in `synopticon hwinfo`.
const report = computed(() => {
  const i = info.value
  if (!i) return ''
  const lines = rows.value.map((r) => `- ${r.label}: ${r.value}`)
  for (const [name, v] of packages.value) lines.push(`- ${name}: ${v}`)
  return lines.join('\n')
})

async function copyReport(): Promise<void> {
  try {
    await navigator.clipboard.writeText(report.value)
    copied.value = true
    window.setTimeout(() => (copied.value = false), 2000)
  } catch {
    // Clipboard is unavailable over plain http on some browsers; the table is
    // still selectable by hand.
  }
}

onMounted(async () => {
  try {
    info.value = await getJSON<AboutInfo>('/api/about')
  } catch {
    /* client handles 401; the header still renders from /api/auth/me */
  } finally {
    loaded.value = true
  }
})
</script>

<template>
  <div class="page about-page">
    <section class="card about-hero">
      <img class="hero-mark" src="/img/logo.svg" alt="" aria-hidden="true" />
      <div>
        <h2>Synopticon <span class="muted" v-if="version">v{{ version }}</span></h2>
        <p class="muted">
          Supplements Synology Photos' face recognition: syncs a photo library from a NAS, runs an
          ensemble face pipeline, groups faces by person, cross-references them against Synology's own
          person labels, and writes approved corrections back.
        </p>
        <div class="cta-row">
          <a class="btn btn-action" :href="repo" target="_blank" rel="noopener noreferrer"
            >View on GitHub</a
          >
          <a
            class="btn btn-ghost"
            :href="repo + '/issues/new'"
            target="_blank"
            rel="noopener noreferrer"
            >Report an issue</a
          >
          <a
            class="btn btn-ghost"
            :href="repo + '/blob/main/LICENSE'"
            target="_blank"
            rel="noopener noreferrer"
            >License</a
          >
        </div>
      </div>
    </section>

    <section class="card about-card">
      <div class="card-head">
        <h3>Build &amp; environment</h3>
        <button
          v-if="info"
          class="btn btn-sm"
          type="button"
          @click="copyReport"
          :aria-label="'Copy environment details'"
        >
          {{ copied ? 'Copied' : 'Copy for bug report' }}
        </button>
      </div>
      <table v-if="info" class="data about-table">
        <tbody>
          <tr v-for="r in rows" :key="r.label">
            <th scope="row">{{ r.label }}</th>
            <td :class="{ mono: r.mono }">{{ r.value }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="loaded" class="muted">Environment details are unavailable.</p>
      <p v-else class="muted">Loading…</p>
    </section>

    <section class="card about-card" v-if="packages.length">
      <h3>Installed components</h3>
      <table class="data about-table">
        <tbody>
          <tr v-for="[name, v] in packages" :key="name">
            <th scope="row" class="mono">{{ name }}</th>
            <td class="mono">{{ v }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.about-hero {
  display: flex;
  gap: var(--sp-4);
  align-items: flex-start;
  max-width: 820px;
}
.hero-mark {
  width: 48px;
  height: 48px;
  flex: 0 0 auto;
}
.about-hero h2 {
  margin: 0 0 var(--sp-2);
}
.about-hero p {
  margin: 0;
  max-width: 60ch;
}
.about-card {
  margin-top: var(--sp-4);
  max-width: 820px;
}
.card-head {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}
.card-head h3 {
  flex: 1;
}
.about-table {
  width: 100%;
  margin-top: var(--sp-3);
}
.about-table th {
  width: 200px;
  white-space: nowrap;
  color: var(--text-2);
  font-weight: 500;
}
.about-table td {
  word-break: break-word;
}
</style>
