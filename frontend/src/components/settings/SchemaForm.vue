<script setup lang="ts">
// Schema-driven config form. Fetches GET /api/config once, builds section panels
// from the JSON schema (via the pure utils/schema helpers), tracks a per-field
// model + initial snapshot for dirty detection, and PUTs only the changed keys.
// Ports settings.js's save/reset/dirty/error-mapping logic (the DOM building
// moves into SchemaField). The server is the single validator: a 422 maps
// {loc,msg} errors back onto fields by dotted path (unmatched → toast + tab dot),
// a 409 means a job is running. Masked secrets are never resubmitted (a blank
// secret field = "keep"). Unsaved changes warn on both window unload and
// in-app route navigation.
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import SchemaField from './SchemaField.vue'
import {
  buildSections,
  toControlValue,
  fromControlValue,
  isFieldChanged,
  matchErrorField,
  type SectionDescriptor,
  type FieldDescriptor,
  type ControlValue,
  type ConfigError,
  type JsonSchema,
} from '../../utils/schema'
import { getJSON, putJSON, ApiError } from '../../api/client'
import type { ConfigDoc } from '../../api/types'
import { toast } from '../../stores/toasts'
import { confirm } from '../../composables/useConfirm'

defineProps<{ activeSection: string }>()
const emit = defineEmits<{
  (e: 'update:errorSections', v: string[]): void
  (e: 'activate', section: string): void
  (e: 'loaded'): void
}>()

const loading = ref(true)
const loadError = ref('')
const sections = ref<SectionDescriptor[]>([])
const model = reactive<Record<string, ControlValue>>({})
const initial = reactive<Record<string, ControlValue>>({})
const rawValues = reactive<Record<string, unknown>>({})
const errors = reactive<Record<string, string>>({})
const envSet = ref<Set<string>>(new Set())
const saving = ref(false)
const saveStatus = reactive<{ text: string; cls: string }>({ text: '', cls: '' })

const flatFields = computed<FieldDescriptor[]>(() => sections.value.flatMap((s) => s.fields))

// The status line: an explicit save outcome wins; otherwise a live dirty hint.
const statusText = computed(() =>
  saveStatus.text ? saveStatus.text : dirty.value ? 'Unsaved changes' : '',
)
const statusClass = computed(() =>
  saveStatus.text ? saveStatus.cls : dirty.value ? 'dirty' : '',
)

const dirty = computed(() =>
  flatFields.value.some((f) =>
    isFieldChanged(f.info, model[f.dotted], initial[f.dotted], f.isSecret),
  ),
)

const errorSections = computed(() => {
  const s = new Set<string>()
  for (const dotted of Object.keys(errors)) s.add(dotted.split('.')[0])
  return [...s]
})
watch(errorSections, (v) => emit('update:errorSections', v), { immediate: true })

function clearErrors(): void {
  for (const k of Object.keys(errors)) delete errors[k]
}

function clearModel(): void {
  for (const k of Object.keys(model)) delete model[k]
  for (const k of Object.keys(initial)) delete initial[k]
  for (const k of Object.keys(rawValues)) delete rawValues[k]
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  let cfg: ConfigDoc
  try {
    cfg = await getJSON<ConfigDoc>('/api/config')
  } catch (e) {
    loadError.value =
      'Could not load configuration: ' + ((e as Error).message || 'error')
    loading.value = false
    return
  }
  sections.value = buildSections(cfg.schema as JsonSchema)
  envSet.value = new Set(cfg.env_overrides || [])
  const values = cfg.values || {}
  clearModel()
  clearErrors()
  for (const sec of sections.value) {
    const vals = values[sec.key] || {}
    for (const f of sec.fields) {
      const v = vals[f.key]
      rawValues[f.dotted] = v
      const cv = toControlValue(f.info, v)
      model[f.dotted] = cv
      initial[f.dotted] = cv
    }
  }
  saveStatus.text = ''
  saveStatus.cls = ''
  loading.value = false
  emit('loaded')
}

function buildPartial(): Record<string, Record<string, unknown>> {
  const partial: Record<string, Record<string, unknown>> = {}
  for (const f of flatFields.value) {
    if (!isFieldChanged(f.info, model[f.dotted], initial[f.dotted], f.isSecret)) continue
    ;(partial[f.section] ??= {})[f.key] = fromControlValue(f.info, model[f.dotted])
  }
  return partial
}

function showErrors(errs: ConfigError[]): void {
  const bad: string[] = []
  for (const err of errs) {
    const dotted = matchErrorField(err.loc, flatFields.value)
    if (dotted) {
      errors[dotted] = err.msg
      const sec = dotted.split('.')[0]
      if (!bad.includes(sec)) bad.push(sec)
    } else {
      toast(`${err.loc}: ${err.msg}`, 'error')
    }
  }
  saveStatus.text = 'Please fix the highlighted fields'
  saveStatus.cls = 'err'
  if (bad.length) emit('activate', bad[0])
}

async function save(): Promise<void> {
  clearErrors()
  let partial: Record<string, Record<string, unknown>>
  try {
    partial = buildPartial()
  } catch {
    saveStatus.text = 'Invalid JSON in one of the fields'
    saveStatus.cls = 'err'
    return
  }
  if (Object.keys(partial).length === 0) {
    saveStatus.text = 'Nothing to save'
    saveStatus.cls = ''
    return
  }
  saving.value = true
  try {
    await putJSON('/api/config', partial)
    saveStatus.text = 'Saved. Some changes take effect on restart.'
    saveStatus.cls = 'ok'
    toast('Configuration saved', 'ok')
    await load()
  } catch (err) {
    const errsBody = err instanceof ApiError ? err.body : null
    if (err instanceof ApiError && err.status === 422 && Array.isArray(errsBody?.errors)) {
      showErrors(errsBody!.errors as ConfigError[])
    } else if (err instanceof ApiError && err.status === 409) {
      saveStatus.text = 'A job is running — try again once it finishes'
      saveStatus.cls = 'err'
    } else {
      saveStatus.text = (err as Error).message || 'Save failed'
      saveStatus.cls = 'err'
    }
  } finally {
    saving.value = false
  }
}

function discard(): void {
  void load()
}

// Unsaved-changes guards: window unload (tab close / reload) + in-app nav.
function onBeforeUnload(e: BeforeUnloadEvent): void {
  if (dirty.value) {
    e.preventDefault()
    e.returnValue = ''
  }
}
onMounted(() => {
  window.addEventListener('beforeunload', onBeforeUnload)
  void load()
})
onUnmounted(() => window.removeEventListener('beforeunload', onBeforeUnload))

onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  return confirm({
    title: 'Discard changes?',
    message: 'You have unsaved configuration changes. Leave without saving?',
    okLabel: 'Discard',
  })
})
</script>

<template>
  <p v-if="loading" class="settings-loading">Loading configuration…</p>
  <p v-else-if="loadError" class="settings-loading">{{ loadError }}</p>

  <template v-else>
    <div
      v-for="sec in sections"
      :key="sec.key"
      class="settings-panel"
      v-show="activeSection === sec.key"
    >
      <div class="card">
        <SchemaField
          v-for="f in sec.fields"
          :key="f.dotted"
          :field="f"
          v-model="model[f.dotted]"
          :raw-value="rawValues[f.dotted]"
          :env-override="envSet.has(f.dotted)"
          :error="errors[f.dotted] ?? null"
        />
      </div>
    </div>

    <div class="save-bar">
      <button class="btn btn-primary" type="button" :disabled="!dirty || saving" @click="save">
        Save changes
      </button>
      <button class="btn btn-ghost" type="button" @click="discard">Discard</button>
      <span class="save-status" :class="statusClass" aria-live="polite">{{ statusText }}</span>
    </div>
  </template>
</template>

<style scoped>
.settings-loading {
  color: var(--text-2);
}
.settings-panel {
  display: block;
}
.save-bar {
  position: sticky;
  bottom: 0;
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) 0;
  margin-top: var(--sp-4);
  background: var(--bg);
  border-top: 1px solid var(--border-soft);
}
.save-status {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.save-status.dirty {
  color: var(--warn);
}
.save-status.ok {
  color: var(--ok);
}
.save-status.err {
  color: var(--danger);
}
</style>
