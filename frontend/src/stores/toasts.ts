// Module-scoped reactive toast list (no Pinia). Ported from lib.js `toast()`;
// auto-dismisses after 4s, matching the old behaviour.
import { reactive } from 'vue'

export type ToastKind = 'ok' | 'error' | ''

export interface Toast {
  id: number
  message: string
  kind: ToastKind
}

const state = reactive<{ items: Toast[] }>({ items: [] })
let seq = 0

export function toast(message: string, kind: ToastKind = ''): void {
  const id = ++seq
  state.items.push({ id, message, kind })
  window.setTimeout(() => dismiss(id), 4000)
}

export function dismiss(id: number): void {
  const i = state.items.findIndex((t) => t.id === id)
  if (i >= 0) state.items.splice(i, 1)
}

export function useToasts() {
  return { toasts: state, toast, dismiss }
}
