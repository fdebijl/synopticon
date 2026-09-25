<script setup lang="ts">

import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { getJSON } from '../api/client'
import type { ReviewCounts } from '../api/types'
import { useAuth } from '../stores/auth'

interface NavItem {
  id: string
  to: string
  label: string
  separator?: boolean
}

const NAV: NavItem[] = [
  { id: 'dashboard', to: '/', label: 'Dashboard' },
  { id: 'pipeline', to: '/pipeline', label: 'Pipeline' },
  { id: 'review', to: '/review', label: 'Review' },
  { id: 'apply', to: '/apply', label: 'Apply', separator: true },
  { id: 'inspect', to: '/inspect', label: 'Inspect' },
  { id: 'utilities', to: '/utilities', label: 'Utilities' },
  { id: 'schedules', to: '/schedules', label: 'Schedules', separator: true },
  { id: 'maintenance', to: '/maintenance', label: 'Maintenance' },
  { id: 'settings', to: '/settings', label: 'Settings' },
]

const route = useRoute()
const { state: auth } = useAuth()
const pending = ref(0)
let timer: number | null = null
let alive = false

const version = computed(() => auth.me?.version ?? '')

function isActive(to: string): boolean {
  if (to === '/') return route.path === '/'
  return route.path === to || route.path.startsWith(to + '/')
}

// setTimeout-chained rather than setInterval: an interval keeps firing while an
// earlier request is still outstanding, so a slow backend accumulates concurrent
// requests for the same badge. A hidden tab skips its turn entirely.
async function refresh(): Promise<void> {
  if (document.visibilityState === 'hidden') return
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

async function loop(): Promise<void> {
  await refresh()
  if (alive) timer = window.setTimeout(loop, 15000)
}

onMounted(() => {
  alive = true
  void loop()
})
onUnmounted(() => {
  alive = false
  if (timer !== null) window.clearTimeout(timer)
  timer = null
})
</script>

<template>
  <nav class="sidebar" role="navigation" aria-label="Primary">
    <RouterLink to="/" class="brand" @click="closeNav">
      <img class="brand-mark" src="/img/logo.svg" alt="" aria-hidden="true" />
      <span class="brand-name">Synopticon</span>
    </RouterLink>
    <ul class="nav">
      <template v-for="item in NAV" :key="item.id">
        <li>
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
            >
              {{ pending }}
            </span>
          </RouterLink>
        </li>
        <li v-if="item.separator" class="nav-separator" aria-hidden="true"></li>
      </template>
    </ul>
    <div class="sidebar-footer">
      <RouterLink
        to="/about"
        class="nav-item"
        :class="{ active: isActive('/about') }"
        :aria-current="isActive('/about') ? 'page' : undefined"
        @click="closeNav"
      >
        <span class="nav-label">About</span>
        <span v-if="version" class="nav-version muted">v{{ version }}</span>
      </RouterLink>
    </div>
  </nav>
  <div class="nav-scrim" @click="closeNav" aria-hidden="true"></div>
</template>

<style scoped>
.sidebar-footer {
  margin-top: auto;
  padding-top: var(--sp-2);
  border-top: 1px solid var(--border-soft);
}
.nav {
  list-style: none;
  margin: var(--sp-3) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.nav-item {
  display: flex;
  align-items: center;
  height: var(--nav-item-h);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius);
  color: var(--text);
  text-decoration: none;
}
.nav-item:hover {
  background: var(--bg-sunken);
  text-decoration: none;
}
.nav-item.active {
  background: var(--accent-tint);
  color: var(--accent);
  font-weight: 600;
}
.nav-label {
  flex: 1;
}
.nav-badge {
  background: var(--accent);
  color: #fff;
  border-radius: 999px;
  font-size: var(--fs-sm);
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.nav-scrim {
  display: none;
}
.nav-version {
  font-size: var(--fs-sm);
}
.nav-separator {
  height: 1px;
  margin: var(--sp-2) 0;
  background-color: var(--border-soft);
}
</style>
