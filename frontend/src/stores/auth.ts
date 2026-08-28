// Module-scoped reactive auth/session state (no Pinia). `fetchMe` caches the
// /api/auth/me result; the router guard reads the cache, login/logout force a
// refresh so the guard sees fresh state on the next navigation.
import { reactive } from 'vue'
import { getJSON, postJSON } from '../api/client'
import type { LoginStep, LoginVerifyResult, Me } from '../api/types'

const state = reactive<{ me: Me | null; loaded: boolean }>({
  me: null,
  loaded: false,
})

export async function fetchMe(force = false): Promise<Me> {
  if (state.loaded && !force && state.me) return state.me
  const me = await getJSON<Me>('/api/auth/me')
  state.me = me
  state.loaded = true
  return me
}

// Route 2 answers 200 either way: a completed sign-in (cookie set, `ok:true`)
// or a half-finished one waiting on a two-step code (`mfa_required:true`, no
// cookie). Only the former refreshes the cached /api/auth/me — a challenge
// result authenticates nobody yet.
export async function login(username: string, password: string): Promise<LoginStep> {
  const res = await postJSON<{
    ok?: true
    username?: string
    mfa_required?: true
    challenge?: string
    expires_in?: number
  }>('/api/auth/login', { username, password })
  if (res.mfa_required) {
    return { step: 'challenge', challenge: res.challenge as string, expires_in: res.expires_in as number }
  }
  await fetchMe(true)
  return { step: 'session', username: res.username as string }
}

// Route 3 — the second step. `code` is either a six-digit TOTP code or an
// unused backup code; the server tries both, in that order.
export async function verifyLogin(challenge: string, code: string): Promise<LoginVerifyResult> {
  const res = await postJSON<{ ok: true; username: string; recovery_remaining: number }>(
    '/api/auth/login/verify',
    { challenge, code },
  )
  await fetchMe(true)
  return { username: res.username, recovery_remaining: res.recovery_remaining }
}

export async function logout(): Promise<void> {
  try {
    await postJSON('/api/auth/logout')
  } finally {
    state.me = null
    state.loaded = false
  }
}

export function useAuth() {
  return { state, fetchMe, login, logout }
}
