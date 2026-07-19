// Shared API response shapes. Extended in later phases (review items, config
// docs/schema, setup status). Kept to what is typeable today from the backend:
// web/stats.py (gather_stats), web/jobs.py (Job.meta / events), web/app.py.

export interface Me {
  authenticated: boolean
  username: string | null
  first_boot: boolean
  version: string
}

export interface PhotoSpaceStats {
  total: number
  synced: number
  hashed: number
  deleted: number
}

export interface ExtractStats {
  pipeline_version: string | null
  models_ready: boolean
  eligible: number
  processed: number | null
  coverage: number | null
}

export interface ClusterStats {
  run_id: number
  created_at: string
  clusters: number
}

// queue_counts() -> { pending: {kind: n}, approved: {...}, ... }
export type ReviewCounts = Record<string, Record<string, number>>

export interface Stats {
  photos: Record<string, PhotoSpaceStats>
  faces: number
  embeddings: number
  extract: ExtractStats
  cluster: ClusterStats | null
  review: ReviewCounts
  job: {
    current: Job | null
    last: Job | null
  }
}

export interface AuditEntry {
  // audit.tail rows are dicts; shape firmed up in a later phase.
  [k: string]: unknown
}

export type JobState =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export interface Job {
  id: string
  name: string
  params?: Record<string, unknown>
  argv?: string[]
  state: JobState
  created_at?: number | null
  started_at?: number | null
  ended_at?: number | null
  pid?: number | null
  exit_code?: number | null
  error?: string | null
  seq?: number
}

// GET /api/config (web/configio.py::read_config). Secrets in `values` are masked
// to {secret:true, set:bool}; `schema` is Settings.model_json_schema();
// `env_overrides` are dotted `section.key` names shadowed by a SYNOPTICON_* var.
export interface MaskedSecret {
  secret: true
  set: boolean
}

export interface ConfigDoc {
  path: string
  exists: boolean
  values: Record<string, Record<string, unknown>>
  schema: Record<string, unknown>
  env_overrides: string[]
}

// GET /api/auth/keys (web/auth.py::list_api_keys).
export interface ApiKey {
  id: number
  name: string
  key_prefix: string
  created_at: number | null
  last_used_at: number | null
  revoked: boolean
}

export type JobEventKind =
  | 'phase'
  | 'progress'
  | 'log'
  | 'result'
  | 'error'
  | 'final'

export interface JobEvent {
  event: JobEventKind
  seq?: number
  phase?: string
  done?: number
  total?: number
  message?: string
  level?: string
  state?: JobState
  [k: string]: unknown
}
