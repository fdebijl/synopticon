<script setup lang="ts">
// Ports templates/login.html.j2 to a JSON POST /api/auth/login flow, plus the
// two-step sign-in code phase (SEC1), the session-pin banner (SEC4) and the
// login-throttle countdown (SEC5). The post-login redirect re-implements the
// server's _safe_next: relative paths only, rejecting protocol-relative (//)
// and backslash tricks.
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRoute } from 'vue-router'
import router from '../router'
import { login, verifyLogin } from '../stores/auth'
import { ApiError } from '../api/client'
import '../styles/auth.css'

const route = useRoute()

type Phase = 'password' | 'code'
const phase = ref<Phase>('password')

const username = ref('')
const password = ref('')
const code = ref('')
const challenge = ref('')

const error = ref('')
const recovery = ref('')
const busy = ref(false)

// A 429's Retry-After, ticking down on the client so the button re-enables
// itself without another round trip just to find out it is still blocked.
const retryAfter = ref(0)
let retryTimer: ReturnType<typeof setInterval> | null = null

const pinBanner = computed(() =>
  route.query.reason === 'pin'
    ? 'You were signed out because this browser or network changed. If this keeps happening, run "synopticon session-pin off" on the server.'
    : '',
)

function safeNext(): string {
  const raw = route.query.next
  const target = Array.isArray(raw) ? raw[0] : raw
  if (
    typeof target === 'string' &&
    target.startsWith('/') &&
    !target.startsWith('//') &&
    !target.includes('\\')
  ) {
    return target
  }
  return '/'
}

function clearRetryTimer(): void {
  if (retryTimer !== null) {
    clearInterval(retryTimer)
    retryTimer = null
  }
}

function startRetryCountdown(seconds: number): void {
  clearRetryTimer()
  retryAfter.value = Math.max(0, Math.ceil(seconds))
  if (retryAfter.value <= 0) return
  retryTimer = setInterval(() => {
    retryAfter.value -= 1
    if (retryAfter.value <= 0) clearRetryTimer()
  }, 1000)
}

onBeforeUnmount(clearRetryTimer)

function messageFrom(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    return (typeof e.body?.error === 'string' && e.body.error) || fallback
  }
  return fallback
}

// Every 429 in this contract carries the same three-key body; a single
// handler keeps the password and code phases from drifting apart.
function handleRateLimited(e: ApiError): void {
  error.value = messageFrom(e, 'Too many attempts — try again shortly.')
  recovery.value = typeof e.body?.recovery === 'string' ? e.body.recovery : ''
  const retry = typeof e.body?.retry_after === 'number' ? e.body.retry_after : 30
  startRetryCountdown(retry)
}

// A restart:true 401 means the challenge timed out or ran out of attempts —
// back to the password step, with the username kept (retyping it after a
// five-minute wait reads as a crash) and the password cleared.
function backToPassword(): void {
  phase.value = 'password'
  password.value = ''
  code.value = ''
  challenge.value = ''
}

async function submitPassword(): Promise<void> {
  error.value = ''
  recovery.value = ''
  busy.value = true
  try {
    const result = await login(username.value.trim(), password.value)
    if (result.step === 'challenge') {
      challenge.value = result.challenge
      code.value = ''
      phase.value = 'code'
    } else {
      void router.push(safeNext())
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 429) {
      handleRateLimited(e)
    } else {
      error.value = messageFrom(e, 'Invalid username or password.')
    }
  } finally {
    busy.value = false
  }
}

async function submitCode(): Promise<void> {
  error.value = ''
  recovery.value = ''
  busy.value = true
  try {
    await verifyLogin(challenge.value, code.value.trim())
    void router.push(safeNext())
  } catch (e) {
    if (e instanceof ApiError && e.status === 429) {
      handleRateLimited(e)
    } else if (e instanceof ApiError && e.body?.restart === true) {
      error.value = messageFrom(e, 'Sign in timed out — start again.')
      backToPassword()
    } else {
      error.value = messageFrom(e, 'That code was not accepted.')
      code.value = ''
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="auth-body">
    <div class="auth-card">
      <div class="brand brand-lg">
        <img class="brand-mark" src="/img/logo.svg" alt="" aria-hidden="true" />
        <span class="brand-name">Synopticon</span>
      </div>
      <h1 class="auth-title">Sign in</h1>
      <p v-if="pinBanner" class="auth-error" role="status">{{ pinBanner }}</p>
      <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
      <p v-if="recovery" class="auth-hint">{{ recovery }}</p>

      <form v-if="phase === 'password'" class="auth-form" @submit.prevent="submitPassword">
        <label for="username">Username</label>
        <input
          id="username"
          v-model="username"
          autocomplete="username"
          required
          autofocus
        />
        <label for="password">Password</label>
        <input
          id="password"
          v-model="password"
          type="password"
          autocomplete="current-password"
          required
        />
        <button
          type="submit"
          class="btn btn-action btn-block"
          :disabled="busy || retryAfter > 0"
        >
          {{ retryAfter > 0 ? `Try again in ${retryAfter}s` : 'Sign in' }}
        </button>
      </form>

      <form v-else class="auth-form" @submit.prevent="submitCode">
        <label for="code">Two-step code</label>
        <input
          id="code"
          v-model="code"
          type="text"
          autocomplete="one-time-code"
          required
          autofocus
        />
        <p class="auth-hint">You can use one of your backup codes here instead.</p>
        <button
          type="submit"
          class="btn btn-action btn-block"
          :disabled="busy || retryAfter > 0"
        >
          {{ retryAfter > 0 ? `Try again in ${retryAfter}s` : 'Verify' }}
        </button>
        <button type="button" class="btn btn-ghost btn-block" @click="backToPassword">
          Start over
        </button>
      </form>
    </div>
  </main>
</template>

<style scoped>
.auth-hint {
  color: var(--text-2);
  font-size: var(--fs-sm);
  margin: 0;
}
</style>
