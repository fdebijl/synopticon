// Pure JSON-schema helpers for the schema-driven Settings form. Ported from the
// vanilla `static/js/settings.js` deref/classify/humanize logic plus the
// section/field ordering it hard-codes. No Vue imports — every function here is
// a pure transform over the `GET /api/config` schema + values, so it is
// unit-testable in isolation (a later phase may add vitest coverage).
//
// The backend serialises `Settings.model_json_schema()` (pydantic v2): nested
// config models appear under `$defs` and are referenced from the root
// `properties` via `$ref` (sometimes wrapped in a single-item `allOf`). Optional
// / nullable fields use `anyOf` with a `{"type": "null"}` member, and SecretStr
// fields carry `format: "password"`.

/** A (possibly unresolved) JSON-schema node. Loosely typed on purpose. */
export interface JsonSchema {
  $ref?: string
  $defs?: Record<string, JsonSchema>
  allOf?: JsonSchema[]
  anyOf?: JsonSchema[]
  enum?: unknown[]
  type?: string
  format?: string
  properties?: Record<string, JsonSchema>
  items?: JsonSchema
  description?: string
  [k: string]: unknown
}

/** Control kinds a resolved property schema maps to. */
export type FieldKind =
  | 'password'
  | 'enum'
  | 'boolean'
  | 'integer'
  | 'number'
  | 'array'
  | 'object'
  | 'string'

/** Classification of a resolved property schema into a renderable control. */
export interface FieldInfo {
  kind: FieldKind
  /** enum option values (kind === 'enum'). */
  options?: unknown[]
  /** true when the field also accepts null (from an `anyOf` null member). */
  nullable?: boolean
  /** element type for array controls (kind === 'array'). */
  itemType?: string
  /** enum values for array elements, when present. */
  itemEnum?: unknown[]
}

/** A single renderable config field, section-qualified. */
export interface FieldDescriptor {
  section: string
  key: string
  /** dotted `section.key` path — used for env-override + 422 error mapping. */
  dotted: string
  info: FieldInfo
  /** `Field(title=...)` from the pydantic schema; pydantic auto-fills a humanized
   *  key when unset, so this is normally present. */
  title?: string
  description?: string
  /** `json_schema_extra={"details": ...}` — the technical half of the help text,
   *  rendered as a collapsed, de-emphasized block under the description. */
  details?: string
  isSecret: boolean
}

/** A settings tab: a config section with its ordered fields. */
export interface SectionDescriptor {
  key: string
  label: string
  fields: FieldDescriptor[]
}

/** Config sections rendered as tabs, in display order (mirrors settings.js). */
export const SECTIONS = [
  'nas',
  'storage',
  'database',
  'inference',
  'detection',
  'restoration',
  'clustering',
  'crossref',
] as const

/** Human labels for tabs (config sections + the pseudo Access tab). */
export const LABELS: Record<string, string> = {
  nas: 'NAS',
  storage: 'Storage',
  database: 'Database',
  inference: 'Inference',
  detection: 'Face detection',
  restoration: 'Restoration',
  clustering: 'Face grouping',
  crossref: 'Crossref',
  models: 'Models',
  access: 'Access',
}

/** Dereference a schema node (`$ref` / single-item `allOf`) against `$defs`. */
export function deref(node: JsonSchema | undefined, root: JsonSchema): JsonSchema {
  if (!node) return {}
  if (node.$ref) {
    const name = node.$ref.split('/').pop() as string
    return deref((root.$defs || {})[name] || {}, root)
  }
  if (node.allOf && node.allOf.length === 1) return deref(node.allOf[0], root)
  return node
}

/** Pick a control kind from a resolved property schema. */
export function classify(p: JsonSchema): FieldInfo {
  if (p.format === 'password') return { kind: 'password' }
  if (Array.isArray(p.enum)) return { kind: 'enum', options: p.enum }
  if (p.anyOf) {
    const real = p.anyOf.filter((a) => a.type !== 'null')
    const nullable = p.anyOf.some((a) => a.type === 'null')
    const inner = real[0] || {}
    const c = classify(inner)
    c.nullable = nullable
    return c
  }
  switch (p.type) {
    case 'boolean':
      return { kind: 'boolean' }
    case 'integer':
      return { kind: 'integer' }
    case 'number':
      return { kind: 'number' }
    case 'array':
      return {
        kind: 'array',
        itemType: (p.items && p.items.type) || 'string',
        itemEnum: p.items && p.items.enum,
      }
    case 'object':
      return { kind: 'object' }
    default:
      return { kind: 'string' }
  }
}

/** Turn a snake_case key into a Title Cased label. */
export function humanize(key: string): string {
  return key.replace(/_/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase())
}

/** The `SYNOPTICON_<SECTION>__<KEY>` env var that shadows a dotted key. */
export function envVarName(section: string, key: string): string {
  return `SYNOPTICON_${section.toUpperCase()}__${key.toUpperCase()}`
}

/** Ordered, classified fields for one config section. */
export function sectionFields(root: JsonSchema, section: string): FieldDescriptor[] {
  const sectionSchema = deref(root.properties?.[section], root)
  const props = sectionSchema.properties || {}
  return Object.keys(props).map((key) => {
    const resolved = deref(props[key], root)
    const info = classify(resolved)
    // Prefer the property's own title: deref'ing a $ref would otherwise pick up
    // the referenced model/enum's class-name title instead of the field's.
    const title = props[key].title ?? resolved.title
    return {
      section,
      key,
      dotted: `${section}.${key}`,
      info,
      title: typeof title === 'string' ? title : undefined,
      description: typeof resolved.description === 'string' ? resolved.description : undefined,
      details: typeof resolved.details === 'string' ? resolved.details : undefined,
      isSecret: info.kind === 'password',
    }
  })
}

/** Build every settings section (config tabs only — Access is handled apart). */
export function buildSections(root: JsonSchema): SectionDescriptor[] {
  return SECTIONS.map((section) => ({
    key: section,
    label: LABELS[section] || section,
    fields: sectionFields(root, section),
  }))
}

/** A form control model value: a string for inputs/selects, a bool for checks. */
export type ControlValue = string | boolean

/**
 * The initial control state for a field, given its config value.
 *
 * Secrets always start empty (the plaintext is never sent to the client), so a
 * blank field means "keep". Arrays render comma-joined, objects as pretty JSON,
 * scalars as their string form. Mirrors settings.js's `initial` computation.
 */
export function toControlValue(info: FieldInfo, value: unknown): ControlValue {
  switch (info.kind) {
    case 'password':
      return ''
    case 'boolean':
      return !!value
    case 'array':
      return ((value as unknown[]) || []).join(', ')
    case 'object':
      return JSON.stringify(value == null ? {} : value, null, 2)
    default:
      return value == null ? '' : String(value)
  }
}

/**
 * Parse a control's model value back into a config value. May throw on invalid
 * JSON for object fields (the caller surfaces this as "Invalid JSON").
 */
export function fromControlValue(info: FieldInfo, raw: ControlValue): unknown {
  switch (info.kind) {
    case 'password':
      return raw as string
    case 'boolean':
      return !!raw
    case 'array': {
      const parts = String(raw)
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
      if (info.itemType === 'number' || info.itemType === 'integer') return parts.map(Number)
      return parts
    }
    case 'object':
      return JSON.parse((raw as string) || '{}')
    case 'integer':
    case 'number': {
      const s = String(raw)
      if (s.trim() === '') return info.nullable ? null : ''
      return info.kind === 'integer' ? parseInt(s, 10) : Number(s)
    }
    default:
      return raw as string
  }
}

/**
 * Whether a field's current control value differs from its initial snapshot.
 * Secrets count as changed only when a new value has been typed (so a masked
 * secret is never resubmitted).
 */
export function isFieldChanged(
  info: FieldInfo,
  raw: ControlValue,
  initial: ControlValue,
  isSecret: boolean,
): boolean {
  if (isSecret) return String(raw).length > 0
  if (info.kind === 'boolean') return raw !== initial
  if (info.kind === 'array' || info.kind === 'object') return raw !== initial
  return String(raw) !== String(initial)
}

/** A field-level validation error from `PUT /api/config` (422). */
export interface ConfigError {
  loc: string
  msg: string
}

/**
 * Match a validation error's dotted `loc` to a field: exact match, or the error
 * targets something nested under the field (e.g. `nas.password.value`). Returns
 * the field's dotted path, or null when nothing matches (a section-level error).
 */
export function matchErrorField(loc: string, fields: FieldDescriptor[]): string | null {
  const f = fields.find((x) => x.dotted === loc || loc.indexOf(x.dotted + '.') === 0)
  return f ? f.dotted : null
}
