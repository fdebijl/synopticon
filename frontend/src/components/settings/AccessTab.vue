<script setup lang="ts">
import PasswordCard from './cards/PasswordCard.vue'
import ApiKeysCard from './cards/ApiKeysCard.vue'
import TwoStepCard from './cards/TwoStepCard.vue'
import NetworkAccessCard from './cards/NetworkAccessCard.vue'
import SignInLogCard from './cards/SignInLogCard.vue'
import SessionPinningCard from './cards/SessionPinningCard.vue'
import ThrottleCard from './cards/ThrottleCard.vue'
</script>

<template>
  <div class="settings-panel access-section">
    <PasswordCard />
    <ApiKeysCard />
    <TwoStepCard />
    <NetworkAccessCard />
    <SignInLogCard />
    <SessionPinningCard />
    <ThrottleCard />
  </div>
</template>

<style scoped>
.access-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
}
</style>