// Theme: auto | light | dark. Ported from lib.js `setTheme`. `auto` clears the
// [data-theme] override so prefers-color-scheme decides; light/dark persist to
// localStorage (the no-flash snippet in index.html reads it before first paint).
import { ref } from 'vue'

export type ThemeMode = 'auto' | 'light' | 'dark'

const STORAGE_KEY = 'syn-theme'

function readStored(): ThemeMode {
  const t = localStorage.getItem(STORAGE_KEY)
  return t === 'light' || t === 'dark' ? t : 'auto'
}

const mode = ref<ThemeMode>(readStored())

export function setTheme(next: ThemeMode): void {
  mode.value = next
  if (next === 'auto') {
    document.documentElement.removeAttribute('data-theme')
    localStorage.removeItem(STORAGE_KEY)
  } else {
    document.documentElement.setAttribute('data-theme', next)
    localStorage.setItem(STORAGE_KEY, next)
  }
}

export function useTheme() {
  return { mode, setTheme }
}
