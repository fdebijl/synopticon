<script setup lang="ts">
// Utilities: interactive tools that act on the library itself rather than on
// local pipeline state (which is what Maintenance is for). QuickMerger talks to
// the NAS directly through /api/quickmerger/*; deduplication runs as a job, so
// it shares the standard JobPanel and the typed-phrase consent gate the server
// re-checks in validate_consent.
import { ref, reactive } from 'vue'
import JobPanel from '../components/JobPanel.vue'
import QuickMerger from '../components/QuickMerger.vue'
import { ApiError } from '../api/client'
import { toast } from '../stores/toasts'
import { confirm } from '../composables/useConfirm'

const PHRASE_DEDUPE = 'delete duplicates'

const panel = ref<InstanceType<typeof JobPanel> | null>(null)
const dedupe = reactive({ exact: true, visual: false, threshold: '' })

function startJob(
  name: string,
  params: Record<string, unknown> = {},
  extra: Record<string, unknown> = {},
): void {
  panel.value?.start(name, params, extra).catch((e: unknown) => {
    if (e instanceof ApiError && e.status === 428) toast('Consent required.', 'error')
    else toast((e as Error).message || 'Failed to start job', 'error')
  })
}

function dedupeParams(): Record<string, unknown> {
  const params: Record<string, unknown> = { exact: dedupe.exact, visual: dedupe.visual }
  const t = dedupe.threshold.trim()
  if (t !== '') params.threshold = t
  return params
}

function dedupePreview(): void {
  if (!dedupe.exact && !dedupe.visual) {
    toast('Pick exact and/or visual.', 'error')
    return
  }
  startJob('dedupe', dedupeParams())
}

async function dedupeApply(): Promise<void> {
  if (!dedupe.exact && !dedupe.visual) {
    toast('Pick exact and/or visual.', 'error')
    return
  }
  const ok = await confirm({
    title: 'Delete duplicate photos',
    message: 'This permanently deletes duplicate photos from the NAS.',
    phrase: PHRASE_DEDUPE,
    okLabel: 'Delete duplicates',
  })
  if (!ok) return
  const params = dedupeParams()
  params.apply = true
  startJob('dedupe', params, { confirm: true, confirm_phrase: PHRASE_DEDUPE })
}
</script>

<template>
  <div class="page util-page">
    <JobPanel ref="panel" />

    <QuickMerger />

    <section class="card util-card">
      <h3>Deduplicate photos</h3>
      <p class="muted">
        Delete duplicate photos from the NAS using stored hashes. Dry run is free; applying deletes
        on the NAS.
      </p>
      <div class="util-opts">
        <label class="opt-check"
          ><input type="checkbox" v-model="dedupe.exact" /> exact (sha256 identical)</label
        >
        <label class="opt-check"
          ><input type="checkbox" v-model="dedupe.visual" /> visual (pHash near-duplicates)</label
        >
        <div class="opt-row">
          <label for="dd-threshold">Hamming threshold</label>
          <input
            class="input input-sm"
            id="dd-threshold"
            type="number"
            min="0"
            max="64"
            placeholder="default"
            v-model="dedupe.threshold"
          />
        </div>
      </div>
      <div class="util-actions">
        <button type="button" class="btn" @click="dedupePreview">Dry run</button>
        <button type="button" class="btn btn-danger" @click="dedupeApply">
          Delete duplicates…
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.util-page {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.util-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.util-card h3 {
  margin: 0;
}
.util-opts {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.util-opts .opt-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.util-opts label {
  font-size: var(--fs-sm);
}
.opt-check {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
}
.util-actions {
  display: flex;
  gap: var(--sp-2);
  padding-top: var(--sp-2);
}
</style>
