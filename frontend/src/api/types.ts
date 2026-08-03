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

// ---------------------------------------------------------------------------
// Review queue (web/review/queries.py::load_review_items item shape). The six
// kinds and every derived field the card renderer / filters rely on. The single
// source of truth ReviewCard.vue renders — replacing the old Jinja macro +
// review.js renderCard() duplication.
export type ReviewKind =
  | 'assign'
  | 'low_confidence'
  | 'reassign'
  | 'merge'
  | 'merge_named'
  | 'new_person'

export type ReviewDecision = 'approve' | 'reject'

export interface ReviewPerson {
  person_id?: number | string | null
  name?: string | null
  space?: string | null
}

// payload_json is loosely shaped — only the fields the UI reads are typed.
export interface ReviewPayload {
  person_id?: number | string | null
  person_name?: string | null
  from_person_id?: number | string | null
  from_person_name?: string | null
  from_similarity?: number | null
  suggested_name?: string | null
  person_a?: ReviewPerson | null
  person_b?: ReviewPerson | null
  space?: string | null
  photo_id?: number | string | null
  face_id?: number | null
  face_ids?: number[]
  [k: string]: unknown
}

export interface ReviewItem {
  item_id: number
  kind: ReviewKind | string
  confidence: number | null
  status: string
  payload: ReviewPayload
  crop: string | null
  item_url: string | null
  person_a_url: string | null
  person_b_url: string | null
  person_url: string | null
  from_person_url: string | null
  new_person_crops: (string | null)[]
  merge_crops_a: string[]
  merge_crops_b: string[]
  unnamed_target: boolean
  unnamed_merge: boolean
  named_merge: boolean
  target_crops: string[]
  target_hidden: boolean
  person_a_hidden: boolean
  person_b_hidden: boolean
}

// Client-side augmentation: a session-local decision (null until acted on) drives
// the decided/dimmed state and the "<kind> · <status>" footer without another
// round-trip. `status` stays the server-loaded value.
export interface ClientReviewItem extends ReviewItem {
  decision: ReviewDecision | null
}

export interface ReviewItemsResponse {
  items: ReviewItem[]
  total: number
  limit: number
  offset: number
}

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

/** Live snapshot attached to a job this server process is running (web/jobs.py
 *  ::_progress_snapshot). Absent for queued jobs and for anything read off disk. */
export interface JobProgressSnapshot {
  phase: string | null
  space: string | null
  done: number | null
  total: number | null
  pct: number | null
}

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
  progress?: JobProgressSnapshot | null
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

// GET /api/setup/status (web/setup_routes.py::_status). Drives the wizard's
// resume logic and prefills the NAS + storage steps (no secrets).
export interface SetupStatus {
  config_file: string | null
  nas_configured: boolean
  models_ready: boolean
  models_missing: string[]
  db_exists: boolean
  photos_synced: number
  extract_done: number
  cluster_runs: number
  account_created: boolean
  nas: {
    url: string
    account: string
    verify_tls: boolean
    spaces: string[]
  }
  storage: {
    data_dir: string
    models_dir: string
    keep_originals: boolean
    originals_cache_gb: number
  }
}

// POST /api/setup/test-connection (syno/probe.py::ProbeResult.to_dict).
export interface ProbeStep {
  name: string
  ok: boolean
  detail: string
}
export interface ProbeResult {
  ok: boolean
  steps: ProbeStep[]
  error: string | null
  [k: string]: unknown
}

// POST /api/setup/check-storage (web/setup_routes.py::_check_storage).
export interface StorageDir {
  ok: boolean
  detail: string
  free_gb: number | null
}
export interface StorageCheckResult {
  ok: boolean
  dirs: Record<string, StorageDir>
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
  /** Emitter wall-clock (seconds). Absent on events replayed from disk. */
  ts?: number
  phase?: string
  done?: number
  total?: number
  message?: string
  level?: string
  state?: JobState
  /** Set on `log` events mirrored from the subprocess console. */
  stream?: 'stdout' | 'stderr'
  /** Set on `final`, and on an `error` synthesized from a non-zero exit. */
  exit_code?: number | null
  /** Which space a phase/progress event belongs to (personal | shared). */
  space?: string
  [k: string]: unknown
}

export interface ModelStatus {
  key: string
  file: string
  present: boolean
  size: number | null
  registered: boolean
  sha256: string | null
  source_url: string | null
  license: string | null
}

export interface ModelsResponse {
  models_dir: string
  items: ModelStatus[]
}

export interface AboutInfo {
  version: string
  repo_url: string
  pipeline_version: string | null
  models_ready: boolean
  python: string
  platform: string
  cpu: {
    available_cores: number
    physical_cores: number
    cgroup_quota: number | null
  }
  paths: {
    data_dir: string
    models_dir: string
    db_path: string
  }
  /** dist name → version, null when the distribution is not installed. */
  packages: Record<string, string | null>
}
