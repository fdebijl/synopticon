<script lang="ts">
/** `data` is the one series hue, `bad` a failure share, `rest`/`faint` de-emphasised buckets. */
export type BarTone = 'data' | 'bad' | 'rest' | 'faint'

export interface BarSegment {
  value: number
  tone: BarTone
  label: string
}

export interface BarItem {
  key: string
  label: string
  /** SPA route. */
  to?: string
  /** External link, opened in a new tab. */
  href?: string
  value: number
  /** Right-hand readout; defaults to the value. */
  valueText?: string
  /** Secondary line under the label. */
  note?: string
  /** Split the bar; segment values should sum to `value`. */
  segments?: BarSegment[]
  tone?: BarTone
}
</script>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  items: BarItem[]
  /** Accessible name of the list. */
  label: string
  /** Scale maximum; defaults to the largest value. */
  max?: number
  legend?: Array<{ tone: BarTone; label: string }>
}>()

const scale = computed(() => props.max ?? Math.max(0, ...props.items.map((i) => i.value)))

function width(v: number): string {
  if (!scale.value || v <= 0) return '0%'
  return (v / scale.value) * 100 + '%'
}

function segments(item: BarItem): BarSegment[] {
  const segs = item.segments?.filter((s) => s.value > 0)
  return segs?.length ? segs : [{ value: item.value, tone: item.tone ?? 'data', label: '' }]
}

function readout(item: BarItem): string {
  return item.valueText ?? item.value.toLocaleString()
}

function title(item: BarItem): string {
  const parts = [`${item.label}: ${readout(item)}`]
  for (const s of item.segments ?? []) {
    if (s.value > 0) parts.push(`${s.label} ${s.value.toLocaleString()}`)
  }
  return parts.join(' · ')
}
</script>

<template>
  <div class="bar-list-wrap">
    <ul v-if="legend?.length" class="legend" aria-hidden="true">
      <li v-for="l in legend" :key="l.tone + l.label">
        <span class="swatch" :class="'tone-' + l.tone"></span>{{ l.label }}
      </li>
    </ul>
    <ul class="bar-list" :aria-label="label">
      <li v-for="item in items" :key="item.key" class="row" :title="title(item)">
        <div class="row-label">
          <RouterLink v-if="item.to" :to="item.to">{{ item.label }}</RouterLink>
          <a v-else-if="item.href" :href="item.href" target="_blank" rel="noopener">{{
            item.label
          }}</a>
          <span v-else>{{ item.label }}</span>
          <span v-if="item.note" class="row-note">{{ item.note }}</span>
        </div>
        <div class="track" aria-hidden="true">
          <div v-if="item.value > 0" class="bar" :style="{ width: width(item.value) }">
            <span
              v-for="(s, i) in segments(item)"
              :key="i"
              class="seg"
              :class="'tone-' + s.tone"
              :style="{ flexGrow: s.value }"
            ></span>
          </div>
        </div>
        <div class="row-value">
          {{ readout(item) }}
          <span class="sr-only" v-if="item.segments?.length">
            ({{
              item.segments
                .filter((s) => s.value > 0)
                .map((s) => `${s.label} ${s.value.toLocaleString()}`)
                .join(', ')
            }})
          </span>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.legend {
  list-style: none;
  margin: 0 0 var(--sp-2);
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-3);
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.legend li {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.swatch {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  display: inline-block;
}
.bar-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.row {
  display: grid;
  grid-template-columns: minmax(0, 11rem) minmax(0, 1fr) auto;
  grid-template-areas: 'label track value';
  align-items: center;
  column-gap: var(--sp-3);
  min-width: 0;
}
.row-label {
  grid-area: label;
  min-width: 0;
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}
.row-label > :first-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-note {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.track {
  grid-area: track;
  min-width: 0;
}
.bar {
  display: flex;
  gap: 2px;
  height: 10px;
  min-width: 0;
}
.seg {
  flex-basis: 0;
  min-width: 2px;
  height: 100%;
}
.seg:last-child {
  border-radius: 0 4px 4px 0;
}
.row-value {
  grid-area: value;
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
}
.tone-data {
  background: var(--action);
}
.tone-bad {
  background: var(--danger);
}
.tone-rest {
  background: var(--text-3);
}
.tone-faint {
  background: color-mix(in srgb, var(--text-3) 40%, transparent);
}

@media (max-width: 560px) {
  .row {
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      'label value'
      'track track';
    row-gap: 4px;
  }
}
</style>
