// Typed fetch wrapper. Relative URLs only (the Vite dev proxy and prod serving
// both keep the API same-origin). Every mutation sends
// `Content-Type: application/json` with a `{}` default body — the backend 415s
// mutating /api calls without it (the CSRF gate), so this must never be skipped.
// A 401 redirects to /login?next=<current route> via the router.
import router from '../router'

export interface ApiErrorBody {
  error?: string
  [k: string]: unknown
}

// Non-OK responses surface as this typed error carrying status + parsed body so
// callers can branch on 422 (validation) / 428 (consent) in later phases.
export class ApiError extends Error {
  status: number
  body: ApiErrorBody | null

  constructor(status: number, body: ApiErrorBody | null) {
    super((body && typeof body.error === 'string' && body.error) || `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

async function parseBody(res: Response): Promise<ApiErrorBody | null> {
  try {
    return (await res.json()) as ApiErrorBody
  } catch {
    return null
  }
}

async function request<T>(url: string, init: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  const body = await parseBody(res)
  if (res.status === 401) {
    const current = router.currentRoute.value
    if (current.name !== 'login') {
      void router.push({ name: 'login', query: { next: current.fullPath } })
    }
    throw new ApiError(401, body)
  }
  if (!res.ok) {
    throw new ApiError(res.status, body)
  }
  return body as T
}

export function getJSON<T>(url: string): Promise<T> {
  return request<T>(url, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  })
}

function mutate<T>(method: string, url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method,
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(body ?? {}),
  })
}

export function postJSON<T>(url: string, body?: unknown): Promise<T> {
  return mutate<T>('POST', url, body)
}

export function putJSON<T>(url: string, body?: unknown): Promise<T> {
  return mutate<T>('PUT', url, body)
}

export function deleteJSON<T>(url: string, body?: unknown): Promise<T> {
  return mutate<T>('DELETE', url, body)
}
