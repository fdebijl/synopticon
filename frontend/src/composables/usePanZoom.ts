// Pan and zoom for a fixed-size viewport whose content is one transformed
// layer: wheel and pinch to zoom around the pointer, drag to pan, plus stepped
// controls. Translation is in viewport pixels and the layer is the viewport's
// own size, so callers can keep positioning children in percentages.
import { computed, onBeforeUnmount, ref, type Ref } from 'vue'

const MIN = 1
const MAX = 8
const STEP = 1.6
/** Pointer travel, in px, past which a gesture is a drag and not a click. */
const DRAG_SLOP = 5

export function usePanZoom(el: Ref<HTMLElement | null>) {
  const scale = ref(MIN)
  const x = ref(0)
  const y = ref(0)
  const dragging = ref(false)
  /** True while the pointer sequence that just ended was a drag, not a tap. */
  const dragged = ref(false)

  const zoomed = computed(() => scale.value > MIN + 1e-3)
  const atMax = computed(() => scale.value >= MAX - 1e-3)
  const transform = computed(
    () => `translate3d(${x.value.toFixed(2)}px, ${y.value.toFixed(2)}px, 0) scale(${scale.value})`,
  )

  function size(): { w: number; h: number } {
    const r = el.value?.getBoundingClientRect()
    return { w: r?.width ?? 0, h: r?.height ?? 0 }
  }

  function clamp(): void {
    const { w, h } = size()
    x.value = Math.min(0, Math.max(w - w * scale.value, x.value))
    y.value = Math.min(0, Math.max(h - h * scale.value, y.value))
  }

  /** Zoom to `next`, holding the viewport-relative point (ax, ay) still. */
  function zoomTo(next: number, ax: number, ay: number): void {
    const k = Math.min(MAX, Math.max(MIN, next))
    if (Math.abs(k - scale.value) < 1e-4) return
    const px = (ax - x.value) / scale.value
    const py = (ay - y.value) / scale.value
    scale.value = k
    x.value = ax - px * k
    y.value = ay - py * k
    clamp()
  }

  function step(factor: number): void {
    const { w, h } = size()
    zoomTo(scale.value * factor, w / 2, h / 2)
  }

  function zoomIn(): void {
    step(STEP)
  }

  function zoomOut(): void {
    step(1 / STEP)
  }

  function reset(): void {
    scale.value = MIN
    x.value = 0
    y.value = 0
  }

  /** Bring a point given as 0..1 of the content to the middle of the viewport. */
  function focus(cx: number, cy: number): void {
    if (!zoomed.value) return
    const { w, h } = size()
    x.value = w / 2 - cx * w * scale.value
    y.value = h / 2 - cy * h * scale.value
    clamp()
  }

  function local(clientX: number, clientY: number): { ax: number; ay: number } {
    const r = el.value?.getBoundingClientRect()
    return { ax: clientX - (r?.left ?? 0), ay: clientY - (r?.top ?? 0) }
  }

  function onWheel(e: WheelEvent): void {
    const lines = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1
    const dy = e.deltaY * lines
    // Zooming out at rest is a no-op, so let that scroll belong to the page —
    // the reader is never trapped inside the photo.
    if (dy > 0 && !zoomed.value) return
    e.preventDefault()
    const { ax, ay } = local(e.clientX, e.clientY)
    // ctrl+wheel is a trackpad pinch, which reports much smaller deltas.
    zoomTo(scale.value * Math.exp(-dy * (e.ctrlKey ? 0.01 : 0.0015)), ax, ay)
  }

  function onDoubleClick(e: MouseEvent): void {
    const { ax, ay } = local(e.clientX, e.clientY)
    if (atMax.value) reset()
    else zoomTo(scale.value * STEP * STEP, ax, ay)
  }

  const pointers = new Map<number, { x: number; y: number }>()
  let pinchDist = 0
  let travel = 0

  function spread(): { dist: number; cx: number; cy: number } {
    const [a, b] = [...pointers.values()]
    return {
      dist: Math.hypot(a.x - b.x, a.y - b.y),
      cx: (a.x + b.x) / 2,
      cy: (a.y + b.y) / 2,
    }
  }

  function onPointerDown(e: PointerEvent): void {
    if (e.pointerType === 'mouse' && e.button !== 0) return
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (pointers.size === 1) {
      travel = 0
      dragged.value = false
    }
    if (pointers.size === 2) pinchDist = spread().dist
    // No pointer capture: it would retarget the click away from the boxes.
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', onPointerUp)
    window.addEventListener('pointercancel', onPointerUp)
  }

  function onPointerMove(e: PointerEvent): void {
    const prev = pointers.get(e.pointerId)
    if (!prev) return
    const dx = e.clientX - prev.x
    const dy = e.clientY - prev.y
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY })
    travel += Math.abs(dx) + Math.abs(dy)
    if (travel > DRAG_SLOP) dragged.value = true

    if (pointers.size >= 2) {
      const { dist, cx, cy } = spread()
      if (pinchDist > 0 && dist > 0) {
        const { ax, ay } = local(cx, cy)
        zoomTo(scale.value * (dist / pinchDist), ax, ay)
      }
      pinchDist = dist
      dragging.value = true
      return
    }
    if (!zoomed.value) return
    x.value += dx
    y.value += dy
    clamp()
    dragging.value = true
  }

  function onPointerUp(e: PointerEvent): void {
    pointers.delete(e.pointerId)
    if (pointers.size < 2) pinchDist = 0
    if (pointers.size) return
    dragging.value = false
    detach()
  }

  function detach(): void {
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', onPointerUp)
    window.removeEventListener('pointercancel', onPointerUp)
  }

  window.addEventListener('resize', clamp)
  onBeforeUnmount(() => {
    detach()
    window.removeEventListener('resize', clamp)
  })

  return {
    scale,
    zoomed,
    atMax,
    dragging,
    dragged,
    transform,
    zoomIn,
    zoomOut,
    reset,
    focus,
    onWheel,
    onDoubleClick,
    onPointerDown,
  }
}
