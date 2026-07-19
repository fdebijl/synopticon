// Confirm dialog state (no Pinia). Ported from lib.js `confirmDialog`.
// `confirm()` returns a Promise<boolean>; the shared ConfirmDialog.vue (mounted
// once in App.vue) renders a native <dialog> off this state. The typed-phrase
// variant disables the OK button until the exact phrase is typed AND a 2s arm
// delay has elapsed (defeats reflexive clicking).
import { reactive } from 'vue'

export interface ConfirmOptions {
  title?: string
  message?: string
  okLabel?: string
  /** Defaults to true (danger styling). Pass false for a non-destructive OK. */
  danger?: boolean
  /** When set, the OK button gates on typing this exact phrase. */
  phrase?: string
}

interface ConfirmState {
  open: boolean
  title: string
  message: string
  okLabel: string
  danger: boolean
  phrase: string | undefined
  resolve: ((ok: boolean) => void) | null
}

const state = reactive<ConfirmState>({
  open: false,
  title: 'Confirm',
  message: '',
  okLabel: 'Confirm',
  danger: true,
  phrase: undefined,
  resolve: null,
})

export function confirm(opts: ConfirmOptions = {}): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    state.title = opts.title ?? 'Confirm'
    state.message = opts.message ?? ''
    state.okLabel = opts.okLabel ?? 'Confirm'
    state.danger = opts.danger !== false
    state.phrase = opts.phrase
    state.resolve = resolve
    state.open = true
  })
}

// Called by ConfirmDialog.vue when the user resolves or dismisses the dialog.
export function settleConfirm(ok: boolean): void {
  const resolve = state.resolve
  state.resolve = null
  state.open = false
  if (resolve) resolve(ok)
}

export function useConfirm() {
  return { state, confirm }
}
