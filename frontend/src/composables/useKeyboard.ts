// Scoped keyboard-shortcut registration. Binds a single `keydown` listener on
// mount and removes it on unmount, so a page's shortcuts only fire while that
// page is alive. Ports review.js's `typingInField()` input-focus guard: events
// are ignored while a text input/textarea/contentEditable has focus (so typing
// "y" in a name field no longer approves the card — a real bug in the legacy
// UI), and while a modal <dialog> is open (the confirm dialog owns the keyboard).
import { onMounted, onUnmounted } from 'vue'

export interface KeyboardOptions {
  /** Skip when a field is focused / a dialog is open. Defaults to true. */
  guard?: boolean
}

function isTypingContext(): boolean {
  const a = document.activeElement as HTMLElement | null
  if (
    a &&
    (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.isContentEditable)
  ) {
    return true
  }
  // A native modal (ConfirmDialog) owns the keyboard while open.
  return document.querySelector('dialog[open]') !== null
}

export function useKeyboard(
  handler: (e: KeyboardEvent) => void,
  options: KeyboardOptions = {},
): void {
  const guard = options.guard !== false

  function onKeydown(e: KeyboardEvent): void {
    if (guard && isTypingContext()) return
    handler(e)
  }

  onMounted(() => document.addEventListener('keydown', onKeydown))
  onUnmounted(() => document.removeEventListener('keydown', onKeydown))
}
