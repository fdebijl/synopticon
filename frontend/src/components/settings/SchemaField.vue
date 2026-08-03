<script setup lang="ts">
// One config field row: label (schema title, else humanized key, + dotted key)
// beside a control
// chosen from the field's classified kind. Ports settings.js::renderField. The
// control model is a string (inputs/selects/textarea) or boolean (checkbox);
// the parent owns the value and dirty tracking, this component is presentational
// plus the env-override chip, field description, and inline 422 error slot.
import { computed } from 'vue'
import { humanize, envVarName, type FieldDescriptor, type ControlValue } from '../../utils/schema'
import type { MaskedSecret } from '../../api/types'

const props = defineProps<{
  field: FieldDescriptor
  modelValue: ControlValue
  /** The raw config value from GET /api/config (masked marker for secrets). */
  rawValue: unknown
  envOverride: boolean
  error: string | null
}>()

const emit = defineEmits<{ (e: 'update:modelValue', v: ControlValue): void }>()

const name = computed(() => props.field.title || humanize(props.field.key))
const kind = computed(() => props.field.info.kind)

// String/select/textarea/number all bind a string model; checkbox binds bool.
const strValue = computed<string>({
  get: () => String(props.modelValue ?? ''),
  set: (v) => emit('update:modelValue', v),
})
const boolValue = computed<boolean>({
  get: () => !!props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const secretPlaceholder = computed(() => {
  const raw = props.rawValue as MaskedSecret | undefined
  return raw && raw.set ? 'set — leave blank to keep' : 'not set'
})

const envMessage = computed(
  () =>
    `overridden by ${envVarName(props.field.section, props.field.key)} — saved value has no effect`,
)
</script>

<template>
  <div class="field-row" :class="{ 'has-error': !!error }">
    <div class="field-label">
      <span class="field-name">{{ name }}</span>
      <span class="field-key">{{ field.dotted }}</span>
    </div>
    <div class="field-control">
      <input
        v-if="kind === 'password'"
        class="input"
        type="password"
        autocomplete="new-password"
        :placeholder="secretPlaceholder"
        v-model="strValue"
      />
      <select v-else-if="kind === 'enum'" class="select" v-model="strValue">
        <option v-for="opt in field.info.options" :key="String(opt)" :value="String(opt)">
          {{ opt }}
        </option>
      </select>
      <input v-else-if="kind === 'boolean'" class="check" type="checkbox" v-model="boolValue" />
      <input
        v-else-if="kind === 'array'"
        class="input"
        type="text"
        placeholder="comma-separated"
        v-model="strValue"
      />
      <textarea v-else-if="kind === 'object'" v-model="strValue"></textarea>
      <input
        v-else-if="kind === 'integer' || kind === 'number'"
        class="input"
        type="number"
        :step="kind === 'number' ? 'any' : undefined"
        v-model="strValue"
      />
      <input v-else class="input" type="text" v-model="strValue" />

      <div v-if="field.description" class="field-desc">{{ field.description }}</div>
      <span v-if="envOverride" class="env-chip">{{ envMessage }}</span>
      <div v-if="error" class="field-error">{{ error }}</div>
    </div>
  </div>
</template>

<style scoped>
.field-row {
  display: grid;
  grid-template-columns: 240px 1fr;
  gap: var(--sp-4);
  align-items: start;
  padding: var(--sp-3) 0;
  border-bottom: 1px solid var(--border-soft);
}
.field-label .field-name {
  font-weight: 500;
}
.field-label .field-key {
  display: block;
  margin-top: 2px;
  font-size: var(--fs-sm);
  color: var(--text-3);
  font-family: ui-monospace, 'SF Mono', Menlo, monospace;
}
.field-control {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}
.field-control .input,
.field-control .select,
.field-control textarea {
  max-width: 420px;
  width: 100%;
}
.field-control textarea {
  font: inherit;
  font-family: ui-monospace, monospace;
  padding: var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-raised);
  color: var(--text);
  min-height: 4.5em;
}
.field-control .check {
  margin: 4px 0 0;
}
.field-desc {
  font-size: var(--fs-sm);
  color: var(--text-2);
  white-space: pre-wrap;
}
.field-error {
  font-size: var(--fs-sm);
  color: var(--danger);
}
.env-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  align-self: flex-start;
  font-size: var(--fs-sm);
  background: rgba(224, 133, 15, 0.16);
  color: var(--warn);
  border-radius: 999px;
  padding: 2px 8px;
}
.field-row.has-error .input,
.field-row.has-error .select,
.field-row.has-error textarea {
  border-color: var(--danger);
}
</style>
