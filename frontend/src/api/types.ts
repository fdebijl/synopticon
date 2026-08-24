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

// Exactly what POST /api/review/{id}/decide accepts. 'hide' is the sticky
// counterpart to 'reject': the next Group faces run re-proposes a rejected
// suggestion but never a hidden one.
export type ReviewDecision = 'approve' | 'reject' | 'hide'

export interface ReviewPerson {
  person_id?: number | string | null
  name?: string | null
  space?: string | null
}

// One person the retarget picker can point an item at (GET /api/review/persons).
export interface ReviewPersonSuggestion {
  space: string
  person_id: number
  name: string
  item_count: number | null
  hidden: boolean
  crops: string[]
}

export interface RetargetResponse {
  item_id: number
  status: string
  kind: string
  person_id: number
  person_name: string | null
  created: number
  skipped: number
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
  size?: number | null
  /** Set when a human picked the target person instead of the pipeline. */
  manual_target?: boolean
  original_person_id?: number | string | null
  /** On a retargeted new_person row: what it became. */
  retargeted_to?: {
    space: string
    person_id: number
    person_name: string | null
    item_ids: number[]
  } | null
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
  /** In-app Inspect route for the photo (raw id, not the deep-link top pick). */
  inspect_url: string | null
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

// ---------------------------------------------------------------------------
// Saved schedules (web/schedules.py + schedule_routes.py). The job catalog is
// served alongside the list rather than duplicated here: the param whitelist
// lives in web/jobs.py, so the form must be described by the server or the two
// drift apart.
export type ScheduleFieldType = 'bool' | 'int' | 'text' | 'select' | 'multiselect'

export interface ScheduleFormField {
  key: string
  label: string
  type: ScheduleFieldType
  options: string[]
  help: string
  default: unknown
}

export interface ScheduleJobForm {
  job: string
  label: string
  description: string
  fields: ScheduleFormField[]
  needs_confirm: boolean
  warning: string
}

export type ScheduleRunStatus = 'submitted' | 'skipped' | 'missed' | 'error'

export interface ScheduleRun {
  id: number
  schedule_id: number
  fired_at: number
  job_id: string | null
  status: ScheduleRunStatus
  detail: string | null
  /** Live state of the job that firing started, when it is still known. */
  job_state?: JobState | null
}

export interface Schedule {
  id: number
  name: string
  job: string
  job_label: string
  params: Record<string, unknown>
  confirm: boolean
  cron: string
  timezone: string | null
  enabled: boolean
  created_at: number
  updated_at: number
  next_run_at: number | null
  last_run_at: number | null
  last_job_id: string | null
  last_status: ScheduleRunStatus | null
  runs?: ScheduleRun[]
}

export interface SchedulesResponse {
  items: Schedule[]
  jobs: ScheduleJobForm[]
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

// -- Inspect (per-photo debug view) ---------------------------------------- #
/** A box normalized to the displayed frame: 0..1 of width/height. */
export interface NormBox {
  x: number
  y: number
  w: number
  h: number
}

export interface InspectPoint {
  x: number
  y: number
}

export interface InspectCluster {
  run_id: number
  cluster_id: number
  size: number | null
  mapped_person_id: number | null
  map_space: string | null
  vote_fraction: number | null
  labeled_count: number | null
  mapped_person_name: string | null
  mapped_person_url: string | null
}

export interface InspectEmbedding {
  model: string
  variant: string
  dim: number
  model_version: string | null
}

/** The Synology box covering one of our detections, when one does. */
export interface InspectFaceSyno {
  syno_face_id: number
  person_id: number | null
  name: string | null
  iou: number
}

export interface InspectFace {
  face_id: number
  detector: string
  /** Pixel coords in the frame we detected against (see `display`). */
  bbox: NormBox
  /** The same box as 0..1, or null when the frame size is unknown. */
  box: NormBox | null
  det_score: number | null
  det_score_secondary: number | null
  quality: number | null
  restored: boolean
  restore_disagreement: number | null
  pipeline_version: string
  created_at: number
  crop_url: string | null
  ctx_crop_url: string | null
  landmarks: InspectPoint[] | null
  embeddings: InspectEmbedding[]
  cluster: InspectCluster | null
  /** Set when Synology has a box over this face — a tag here is a reassign. */
  syno: InspectFaceSyno | null
}

export interface InspectSynoFace {
  syno_face_id: number
  person_id: number | null
  name: string | null
  box: NormBox
  person_url: string | null
  synced_at: number
}

export interface InspectReviewItem {
  item_id: number
  kind: string
  status: string
  confidence: number | null
  created_at: number
  decided_at: number | null
  decided_by: string | null
  face_ids: number[]
  payload: ReviewPayload
}

export interface InspectReport {
  space: string
  photo_id: number
  photo: {
    filename: string | null
    folder_id: number | null
    filesize: number | null
    time: number | null
    indexed_time: number | null
    type: string | null
    cache_key: string | null
    unit_id: number | null
    width: number | null
    height: number | null
    orientation: number | null
    synced_at: number
    deleted: boolean
    sha256: string | null
    phash: string | null
    similar_top_pick: number | null
  }
  display: {
    width: number | null
    height: number | null
    /** Quarter-turn clockwise our boxes need to land on the served photo. */
    rotation: number
    /** How that was decided: `synology-faces` voted, `none` had no evidence. */
    rotation_source: 'synology-faces' | 'none'
  }
  image_url: string
  nas_url: string | null
  linked_photo_id: number
  extract: {
    cache_key: string | null
    pipeline_version: string
    face_count: number
    processed_at: number
    stale: boolean
  } | null
  faces: InspectFace[]
  syno_faces: InspectSynoFace[]
  review_items: InspectReviewItem[]
  detection: {
    scrfd_score: number
    yolo_score: number
    nms_iou: number
    cross_iou: number
    min_face_px: number
    max_long_side: number
    scales: number[]
  }
}

/** POST /api/inspect/face/{id}/assign — what the queue row became. */
export interface InspectAssignResponse {
  face_id: number
  item_id: number
  /** `reassign` when Synology already named this face: a stricter Apply flag. */
  kind: 'assign' | 'reassign'
  status: string
  person_id: number
  person_name: string | null
  /** Queued suggestions about this same face that were hidden in its favour. */
  superseded: number[]
}

export interface InspectMeta {
  spaces: string[]
  pipeline_version: string | null
}
