<script setup lang="ts">
// Ports templates/login.html.j2 to a JSON POST /api/auth/login flow. The
// post-login redirect re-implements the server's _safe_next: relative paths
// only, rejecting protocol-relative (//) and backslash tricks. 401/429 errors
// render inline.
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import router from '../router'
import { login } from '../stores/auth'
import { ApiError } from '../api/client'
import '../styles/auth.css'

const route = useRoute()
const username = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)

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

async function submit(): Promise<void> {
  error.value = ''
  busy.value = true
  try {
    await login(username.value.trim(), password.value)
    void router.push(safeNext())
  } catch (e) {
    if (e instanceof ApiError) {
      error.value =
        (typeof e.body?.error === 'string' && e.body.error) ||
        (e.status === 429
          ? 'Too many attempts — try again shortly.'
          : 'Invalid username or password.')
    } else {
      error.value = 'Sign in failed.'
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
      <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
      <form class="auth-form" @submit.prevent="submit">
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
        <button type="submit" class="btn btn-action btn-block" :disabled="busy">
          Sign in
        </button>
      </form>
    </div>
  </main>
</template>
