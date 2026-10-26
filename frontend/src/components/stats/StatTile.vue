<script setup lang="ts">
import { RouterLink, type RouteLocationRaw } from 'vue-router'

defineProps<{
  label: string
  value: string
  sub?: string
  to?: RouteLocationRaw
}>()
</script>

<template>
  <component :is="to ? RouterLink : 'div'" class="tile" :to="to">
    <div class="tile-label">{{ label }}</div>
    <div class="tile-value">{{ value }}</div>
    <div v-if="sub" class="tile-sub">{{ sub }}</div>
    <slot />
  </component>
</template>

<style scoped>
.tile {
  display: block;
  background: var(--bg-raised);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-lg);
  padding: var(--sp-4);
  box-shadow: var(--shadow-card);
  color: var(--text);
  min-width: 0;
}
a.tile:hover {
  text-decoration: none;
  border-color: var(--border);
}
.tile-label {
  font-size: var(--fs-sm);
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.tile-value {
  font-size: 28px;
  font-weight: 600;
  margin-top: var(--sp-1);
  line-height: 1.1;
  overflow-wrap: anywhere;
}
.tile-sub {
  font-size: var(--fs-sm);
  color: var(--text-2);
  margin-top: 2px;
}
</style>
