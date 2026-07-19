<script setup lang="ts">
import { ref, watch } from 'vue'
import { useConfirm, settleConfirm } from '../composables/useConfirm'

const { state } = useConfirm()
const dlg = ref<HTMLDialogElement | null>(null)
const phraseInput = ref('')
const armed = ref(false)

watch(
  () => state.open,
  (open) => {
    if (open) {
      phraseInput.value = ''
      // No phrase -> immediately armed. With a phrase, arm after 2s.
      armed.value = !state.phrase
      if (state.phrase) {
        window.setTimeout(() => {
          armed.value = true
        }, 2000)
      }
      // Show on the next frame so the <dialog> ref is bound.
      requestAnimationFrame(() => {
        if (dlg.value && !dlg.value.open) dlg.value.showModal()
      })
    } else if (dlg.value?.open) {
      dlg.value.close()
    }
  },
)

function okDisabled(): boolean {
  if (!armed.value) return true
  if (state.phrase) return phraseInput.value !== state.phrase
  return false
}

function onCancel(): void {
  settleConfirm(false)
}

function onOk(): void {
  if (!okDisabled()) settleConfirm(true)
}

// Fired on Escape / backdrop close. Guarded so it does not double-settle when
// we programmatically close after resolving.
function onClose(): void {
  if (state.open) settleConfirm(false)
}
</script>

<template>
  <dialog ref="dlg" class="modal" @cancel.prevent="onCancel" @close="onClose">
    <div class="modal-body">
      <h3 class="modal-title">{{ state.title }}</h3>
      <p v-if="state.message">{{ state.message }}</p>
      <label v-if="state.phrase" class="modal-phrase">
        <span>Type "{{ state.phrase }}" to confirm</span>
        <input v-model="phraseInput" autocomplete="off" />
      </label>
      <div class="modal-actions">
        <button class="btn" type="button" autofocus @click="onCancel">Cancel</button>
        <button
          class="btn"
          type="button"
          :class="state.danger ? 'btn-danger' : 'btn-action'"
          :disabled="okDisabled()"
          @click="onOk"
        >
          {{ state.okLabel }}
        </button>
      </div>
    </div>
  </dialog>
</template>
