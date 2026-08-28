<script setup lang="ts">
// Change the signed-in user's password. POST /api/auth/change-password does the
// old-password check server-side; the confirm field is only a typo guard.
import { reactive } from 'vue'
import { postJSON, ApiError } from '../../../api/client'
import { toast } from '../../../stores/toasts'

const pw = reactive({ current: '', next: '', confirm: '' })

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message || fallback
  return (err as Error)?.message || fallback
}

async function changePassword(): Promise<void> {
  if (pw.next !== pw.confirm) {
    toast('New passwords do not match', 'error')
    return
  }
  try {
    await postJSON('/api/auth/change-password', {
      current_password: pw.current,
      new_password: pw.next,
    })
    pw.current = pw.next = pw.confirm = ''
    toast('Password updated', 'ok')
  } catch (err) {
    toast(errMsg(err, 'Could not change password'), 'error')
  }
}
</script>

<template>
  <div class="card password-section">
    <h3>Change password</h3>
    <form class="password-form" @submit.prevent="changePassword">
      <label>
        <span>Current password</span>
        <input class="input" type="password" autocomplete="current-password" required v-model="pw.current" />
      </label>
      <label>
        <span>New password</span>
        <input class="input" type="password" autocomplete="new-password" required v-model="pw.next" />
      </label>
      <label>
        <span>Confirm new password</span>
        <input class="input" type="password" autocomplete="new-password" required v-model="pw.confirm" />
      </label>
      <button class="btn btn-primary" type="submit">Update password</button>
    </form>
  </div>
</template>

<style scoped>
.password-section {
  max-width: 640px;
}
.password-form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-width: 380px;
  margin-top: var(--sp-3);
}
.password-form label {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.password-form label span {
  margin-right: var(--sp-3);
}
</style>
