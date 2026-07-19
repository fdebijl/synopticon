<script setup lang="ts">
// Primary navigation sidebar. Ports templates/partials/sidebar.html.j2:
// nav items, active highlight (server used `active == id`; mirrored here off the
// route path), the Review pending-count badge (from /api/review/counts), plus a
// version footer sourced from the cached /api/auth/me. The topbar (job chip,
// theme/user menus) lives in App.vue as the shell.
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { getJSON } from '../api/client'
import type { ReviewCounts } from '../api/types'
import { useAuth } from '../stores/auth'

interface NavItem {
  id: string
  to: string
  label: string
}

const NAV: NavItem[] = [
  { id: 'dashboard', to: '/', label: 'Dashboard' },
  { id: 'pipeline', to: '/pipeline', label: 'Pipeline' },
  { id: 'review', to: '/review', label: 'Review' },
  { id: 'apply', to: '/apply', label: 'Apply' },
  { id: 'maintenance', to: '/maintenance', label: 'Maintenance' },
  { id: 'settings', to: '/settings', label: 'Settings' },
]

const route = useRoute()
const { state: auth } = useAuth()
const pending = ref(0)
let timer: number | null = null

const version = computed(() => auth.me?.version ?? '')

function isActive(to: string): boolean {
  if (to === '/') return route.path === '/'
  return route.path === to || route.path.startsWith(to + '/')
}

async function refresh(): Promise<void> {
  try {
    const data = await getJSON<{ counts: ReviewCounts }>('/api/review/counts')
    const p = data.counts.pending ?? {}
    pending.value = Object.values(p).reduce((a, b) => a + b, 0)
  } catch {
    // Non-fatal; the badge simply stays at its last value.
  }
}

function closeNav(): void {
  document.body.classList.remove('nav-open')
}

onMounted(() => {
  void refresh()
  timer = window.setInterval(refresh, 15000)
})
onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer)
})
</script>

<template>
  <nav class="sidebar" role="navigation" aria-label="Primary">
    <RouterLink to="/" class="brand" @click="closeNav">
      <img class="brand-mark" src="/img/logo.svg" alt="" aria-hidden="true" />
      <span class="brand-name">Synopticon</span>
    </RouterLink>
    <ul class="nav">
      <li v-for="item in NAV" :key="item.id">
        <RouterLink
          :to="item.to"
          class="nav-item"
          :class="{ active: isActive(item.to) }"
          :aria-current="isActive(item.to) ? 'page' : undefined"
          @click="closeNav"
        >
          <span class="nav-label">{{ item.label }}</span>
          <span
            v-if="item.id === 'review' && pending"
            class="nav-badge"
            :aria-label="`${pending} pending`"
            >{{ pending }}</span
          >
        </RouterLink>
      </li>
    </ul>
    <div class="sidebar-footer muted" v-if="version">v{{ version }}</div>
  </nav>
  <div class="nav-scrim" @click="closeNav" aria-hidden="true"></div>
</template>

<style scoped>
.sidebar-footer {
  margin-top: auto;
  padding: var(--sp-2) var(--sp-3);
}
</style>
