// Module-scoped reactive auth/session state (no Pinia). `fetchMe` caches the
// /api/auth/me result; the router guard reads the cache, login/logout force a
// refresh so the guard sees fresh state on the next navigation.
import { reactive } from 'vue'
import { getJSON, postJSON } from '../api/client'
import type { Me } from '../api/types'

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

export async function login(username: string, password: string): Promise<void> {
  await postJSON('/api/auth/login', { username, password })
  await fetchMe(true)
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
