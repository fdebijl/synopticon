<script setup lang="ts">
// Placeholder card shown while a page of review items is in flight. Same .card
// chrome as ReviewCard so real cards replace it without a layout shift; sizing
// (grid vs focus-current) comes from the same CSS context as the real card.
</script>

<template>
  <div class="card card-skeleton" aria-hidden="true">
    <div class="skeleton skeleton-img"></div>
    <div class="skeleton skeleton-line"></div>
    <div class="skeleton skeleton-line"></div>
    <div class="card-actions">
      <div class="skeleton skeleton-btn"></div>
      <div class="skeleton skeleton-btn"></div>
    </div>
  </div>
</template>

<style scoped>
.card-skeleton {
  pointer-events: none;
}
.skeleton-img {
  width: 96px;
  height: 96px;
}
.skeleton-line {
  height: 12px;
  margin-top: var(--sp-2);
  width: 70%;
}
.skeleton-line + .skeleton-line {
  width: 45%;
}
.skeleton-btn {
  width: 72px;
  height: 26px;
}
/* Mirrors ReviewCard's action row so the swap-in doesn't reflow. */
.card-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
}
</style>
