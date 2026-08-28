// Translates web_auth_log's protocol identifiers -- event, outcome, reason --
// into the plain-language labels CLAUDE.md's translation rule requires: none
// of the three may reach the DOM raw. Mirrors jobs.ts's jobLabel(): an unknown
// value falls back to itself rather than disappearing, so a reason added on
// the backend before this map catches up still renders as *something*.

const EVENT_LABELS: Record<string, string> = {
  login: 'Password sign-in',
  login_code: 'Two-step code',
  logout: 'Sign-out',
  create_account: 'Account created',
  password_change: 'Password change',
  security_change: 'Security setting changed',
  api_key: 'API key',
}

const OUTCOME_LABELS: Record<string, string> = {
  success: 'Success',
  failure: 'Failed',
  blocked: 'Blocked',
  pending: 'Awaiting code',
}

const REASON_LABELS: Record<string, string> = {
  rate_limited: 'Rate limited',
  password_ok: 'Password correct — code required',
  password_bad: 'Password incorrect — code required',
  bad_credentials: 'Incorrect username or password',
  missing_credentials: 'Username or password missing',
  unknown_challenge: 'Sign-in session expired',
  bad_code: 'Incorrect two-step code',
  bad_password: 'Incorrect password',
  totp_enrolled: 'Two-step sign-in turned on',
  totp_disabled: 'Two-step sign-in turned off',
  recovery_codes_regenerated: 'Backup codes regenerated',
  session_pin_changed: 'Session pinning changed',
  reauth_failed: 'Password or code not accepted',
  unknown_or_revoked_key: 'Unknown or revoked API key',
}

export function eventLabel(event: string | null | undefined): string {
  if (!event) return ''
  return EVENT_LABELS[event] ?? event
}

export function outcomeLabel(outcome: string | null | undefined): string {
  if (!outcome) return ''
  return OUTCOME_LABELS[outcome] ?? outcome
}

export function reasonLabel(reason: string | null | undefined): string {
  if (!reason) return ''
  return REASON_LABELS[reason] ?? reason
}
