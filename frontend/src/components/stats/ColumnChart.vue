<script lang="ts">
export interface Column {
  /** Full label: the tooltip and the table view. */
  label: string
  value: number
  /** Shorter axis label; defaults to `label`. */
  axis?: string
}
</script>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { compact } from '../../utils/format'

const props = withDefaults(
  defineProps<{
    columns: Column[]
    /** What is plotted — the accessible name and the table caption. */
    label: string
    /** Unit appended to readouts: "1,204 photos". */
    unit?: string
    variant?: 'column' | 'area'
    /** Plot height in px, excluding the x-axis band. */
    height?: number
    /** Which column carries a direct value label. */
    highlight?: 'max' | 'last' | 'none'
    emptyText?: string
  }>(),
  {
    unit: '',
    variant: 'column',
    height: 140,
    highlight: 'max',
    emptyText: 'Nothing to chart yet.',
  },
)

const plot = ref<HTMLElement | null>(null)
const inner = ref<HTMLElement | null>(null)
const plotWidth = ref(600)
const active = ref<number | null>(null)

watch(plot, (el, _old, onCleanup) => {
  if (!el || typeof ResizeObserver === 'undefined') return
  const ro = new ResizeObserver((entries) => {
    const w = entries[0]?.contentRect.width
    if (w) plotWidth.value = w
  })
  ro.observe(el)
  onCleanup(() => ro.disconnect())
})

const n = computed(() => props.columns.length)
const maxValue = computed(() => Math.max(0, ...props.columns.map((c) => c.value)))
const hasData = computed(() => maxValue.value > 0)

function niceStep(raw: number): number {
  const mag = 10 ** Math.floor(Math.log10(raw))
  const f = raw / mag
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10
  return nice * mag
}

const ticks = computed<number[]>(() => {
  const max = maxValue.value
  if (max <= 0) return [0]
  const step = Math.max(1, niceStep(max / 3))
  const out: number[] = []
  for (let t = 0; t < max + step; t += step) {
    out.push(t)
    if (t >= max) break
  }
  return out
})

const scale = computed(() => ticks.value[ticks.value.length - 1] || 1)

function pct(v: number): number {
  return (Math.max(0, v) / scale.value) * 100
}

function x(i: number): number {
  return ((i + 0.5) / Math.max(1, n.value)) * 100
}

function fmt(v: number): string {
  return v.toLocaleString() + (props.unit ? ' ' + props.unit : '')
}

const labelled = computed<number | null>(() => {
  if (!hasData.value || props.highlight === 'none') return null
  if (props.highlight === 'last') return n.value - 1
  let best = 0
  props.columns.forEach((c, i) => {
    if (c.value > props.columns[best].value) best = i
  })
  return best
})

const labelEvery = computed(() => {
  const longest = Math.max(1, ...props.columns.map((c) => (c.axis ?? c.label).length))
  const perLabel = longest * 6.5 + 10
  const fit = Math.max(1, Math.floor(plotWidth.value / perLabel))
  return Math.max(1, Math.ceil(n.value / fit))
})

function showAxisLabel(i: number): boolean {
  return i % labelEvery.value === 0
}

const summary = computed(() => {
  if (!hasData.value) return `${props.label}: no data`
  const top = props.columns.reduce((a, c) => (c.value > a.value ? c : a))
  const first = props.columns[0]
  const last = props.columns[n.value - 1]
  return (
    `${props.label}, ${n.value} values from ${first.label} to ${last.label}. ` +
    `Highest: ${top.label} at ${fmt(top.value)}. Use the arrow keys to read each value.`
  )
})

const activeColumn = computed(() => (active.value === null ? null : props.columns[active.value]))

const dotIndex = computed(() => active.value ?? labelled.value)

const tipAlign = computed(() => {
  if (active.value === null) return 'center'
  const pos = (active.value + 0.5) / n.value
  return pos < 0.15 ? 'start' : pos > 0.85 ? 'end' : 'center'
})

const areaPaths = computed(() => {
  if (props.variant !== 'area' || n.value === 0) return { line: '', fill: '' }
  const pts = props.columns.map((c, i) => [i + 0.5, 100 - pct(c.value)] as const)
  const line = pts.map(([px, py], i) => `${i ? 'L' : 'M'}${px},${py}`).join(' ')
  const fill = `${line} L${pts[pts.length - 1][0]},100 L${pts[0][0]},100 Z`
  return { line, fill }
})

function onPointer(e: PointerEvent): void {
  const el = inner.value
  if (!el || !n.value) return
  const r = el.getBoundingClientRect()
  const i = Math.floor(((e.clientX - r.left) / r.width) * n.value)
  active.value = Math.min(n.value - 1, Math.max(0, i))
}

function onKey(e: KeyboardEvent): void {
  if (!n.value) return
  const cur = active.value ?? labelled.value ?? 0
  let next = cur
  if (e.key === 'ArrowRight') next = Math.min(n.value - 1, cur + 1)
  else if (e.key === 'ArrowLeft') next = Math.max(0, cur - 1)
  else if (e.key === 'Home') next = 0
  else if (e.key === 'End') next = n.value - 1
  else return
  e.preventDefault()
  active.value = next
}
</script>

<template>
  <figure class="chart">
    <p v-if="!hasData" class="chart-empty muted">{{ emptyText }}</p>
    <template v-else>
      <div class="frame" :style="{ '--plot-h': height + 'px' }">
        <div class="y-axis" aria-hidden="true">
          <div class="y-inner">
            <span v-for="t in ticks" :key="t" :style="{ bottom: pct(t) + '%' }">{{ compact(t) }}</span>
          </div>
        </div>
        <div
          ref="plot"
          class="plot"
          tabindex="0"
          role="img"
          :aria-label="summary"
          @pointermove="onPointer"
          @pointerleave="active = null"
          @focus="active = labelled ?? 0"
          @blur="active = null"
          @keydown="onKey"
        >
          <div ref="inner" class="inner" :class="{ hovering: active !== null }">
            <div
              v-for="t in ticks"
              :key="t"
              class="grid-line"
              :class="{ base: t === 0 }"
              :style="{ bottom: pct(t) + '%' }"
            ></div>

            <div v-if="variant === 'column'" class="cols">
              <div v-for="(c, i) in columns" :key="i" class="col" :class="{ active: active === i }">
                <div class="bar" :style="{ height: pct(c.value) + '%' }">
                  <span v-if="labelled === i" class="cap">{{ c.value.toLocaleString() }}</span>
                </div>
              </div>
            </div>

            <template v-else>
              <svg class="area" :viewBox="`0 0 ${columns.length} 100`" preserveAspectRatio="none">
                <path class="area-fill" :d="areaPaths.fill" />
                <path class="area-line" :d="areaPaths.line" vector-effect="non-scaling-stroke" />
              </svg>
              <div v-if="active !== null" class="crosshair" :style="{ left: x(active) + '%' }"></div>
              <span
                v-if="dotIndex !== null"
                class="dot"
                :style="{ left: x(dotIndex) + '%', bottom: pct(columns[dotIndex].value) + '%' }"
              ></span>
              <span
                v-if="labelled !== null && active === null"
                class="end-label"
                :style="{ bottom: pct(columns[labelled].value) + '%' }"
                >{{ compact(columns[labelled].value) }}</span
              >
            </template>

            <div
              v-if="activeColumn && active !== null"
              class="tip"
              :class="'tip-' + tipAlign"
              :style="{ left: x(active) + '%' }"
            >
              <strong>{{ fmt(activeColumn.value) }}</strong>
              <span>{{ activeColumn.label }}</span>
            </div>
          </div>
        </div>
        <div class="x-axis" aria-hidden="true">
          <span v-for="(c, i) in columns" :key="i" class="x-label">
            <span v-if="showAxisLabel(i)">{{ c.axis ?? c.label }}</span>
          </span>
        </div>
      </div>
      <p class="sr-only" aria-live="polite">
        {{ activeColumn ? `${activeColumn.label}: ${fmt(activeColumn.value)}` : '' }}
      </p>
      <details class="table-view">
        <summary>Show as table</summary>
        <table class="data">
          <caption class="sr-only">{{ label }}</caption>
          <tbody>
            <tr v-for="(c, i) in columns" :key="i">
              <th scope="row">{{ c.label }}</th>
              <td>{{ fmt(c.value) }}</td>
            </tr>
          </tbody>
        </table>
      </details>
    </template>
  </figure>
</template>

<style scoped>
.chart {
  margin: 0;
  min-width: 0;
}
.chart-empty {
  margin: 0;
  padding: var(--sp-4) 0;
}
.frame {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-rows: var(--plot-h) auto;
  column-gap: var(--sp-2);
}
.y-axis {
  position: relative;
  min-width: 24px;
}
.y-inner,
.inner {
  position: absolute;
  inset: 18px 0 0 0;
}
.y-axis span {
  position: absolute;
  right: 0;
  transform: translateY(50%);
  font-size: var(--fs-sm);
  color: var(--text-2);
  font-variant-numeric: tabular-nums;
  line-height: 1;
  white-space: nowrap;
}
.plot {
  position: relative;
  min-width: 0;
  touch-action: pan-y;
  border-radius: var(--radius);
}
.grid-line {
  position: absolute;
  left: 0;
  right: 0;
  height: 1px;
  background: var(--border-soft);
}
.grid-line.base {
  background: var(--border);
}
.cols {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: flex-end;
}
.col {
  flex: 1 1 0;
  min-width: 0;
  height: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}
.bar {
  position: relative;
  width: min(24px, calc(100% - 2px));
  background: var(--action);
  border-radius: 4px 4px 0 0;
  transition: opacity 0.12s;
}
.inner.hovering .col:not(.active) .bar {
  opacity: 0.55;
}
.cap {
  position: absolute;
  bottom: 100%;
  left: 50%;
  transform: translateX(-50%);
  margin-bottom: 2px;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  line-height: 1.2;
}
.area {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
}
.area-fill {
  fill: var(--action);
  opacity: 0.1;
}
.area-line {
  fill: none;
  stroke: var(--action);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.crosshair {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--border);
}
.dot {
  position: absolute;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--action);
  box-shadow: 0 0 0 2px var(--bg-raised);
  transform: translate(-50%, 50%);
}
.end-label {
  position: absolute;
  right: 0;
  transform: translate(0, -6px);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text);
  line-height: 1;
}
.tip {
  position: absolute;
  top: -18px;
  z-index: 2;
  display: flex;
  flex-direction: column;
  padding: 4px var(--sp-2);
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-menu);
  font-size: var(--fs-sm);
  line-height: 1.3;
  white-space: nowrap;
  pointer-events: none;
  transform: translate(-50%, -100%);
}
.tip-start {
  transform: translate(-12px, -100%);
}
.tip-end {
  transform: translate(calc(-100% + 12px), -100%);
}
.tip strong {
  font-size: var(--fs-base);
  color: var(--text);
}
.tip span {
  color: var(--text-2);
}
.x-axis {
  grid-column: 2;
  display: flex;
  height: 20px;
}
.x-label {
  flex: 1 1 0;
  min-width: 0;
  position: relative;
}
.x-label > span {
  position: absolute;
  top: 4px;
  left: 50%;
  transform: translateX(-50%);
  font-size: var(--fs-sm);
  line-height: 1.2;
  color: var(--text-2);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.table-view {
  margin-top: var(--sp-2);
  font-size: var(--fs-sm);
}
.table-view summary {
  cursor: pointer;
  color: var(--text-2);
}
.table-view table {
  margin-top: var(--sp-2);
  font-size: var(--fs-sm);
}
.table-view th {
  font-weight: 400;
  color: var(--text);
}
.table-view td {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
</style>
