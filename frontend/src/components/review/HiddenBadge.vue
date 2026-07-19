<script setup lang="ts">
// "Hidden on Synology Photos" eye-with-slash badge. Ports the `hidden_badge`
// Jinja macro / review.js HIDDEN_BADGE constant verbatim (same SVG, same
// classes) so the CSS in app.css (.hidden-badge / .name-hidden) matches.
defineProps<{ cls?: string }>()
</script>

<template>
  <span
    class="hidden-badge"
    :class="cls"
    title="Hidden on Synology Photos"
    aria-label="Person hidden on Synology Photos"
  >
    <svg
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path
        d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"
      />
      <line x1="1" y1="1" x2="23" y2="23" />
    </svg>
  </span>
</template>
