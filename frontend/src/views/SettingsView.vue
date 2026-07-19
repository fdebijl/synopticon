<script setup lang="ts">
// Settings page: a single tab bar over the config sections plus an Access tab.
// Ports settings.html.j2's tabs layout. The config tabs are driven by SchemaForm
// (schema-driven form + save bar + dirty guard); the Access tab hosts the
// password / API-key forms. A section tab shows an error dot when SchemaForm
// reports a 422 field error in it. SchemaForm stays mounted across tab switches
// so its dirty state (and the unsaved-changes guard) survives navigation within
// the page.
import { ref } from 'vue'
import SchemaForm from '../components/settings/SchemaForm.vue'
import AccessTab from '../components/settings/AccessTab.vue'
import { SECTIONS, LABELS } from '../utils/schema'

const TABS: string[] = [...SECTIONS, 'access']
const activeTab = ref<string>(SECTIONS[0])
const errorSections = ref<string[]>([])

function activate(tab: string): void {
  activeTab.value = tab
}
</script>

<template>
  <div class="page">
    <div class="settings-tabs" role="tablist" aria-label="Settings sections">
      <button
        v-for="tab in TABS"
        :key="tab"
        class="settings-tab"
        type="button"
        role="tab"
        :class="{ active: activeTab === tab, 'has-error': errorSections.includes(tab) }"
        :aria-selected="activeTab === tab ? 'true' : 'false'"
        @click="activate(tab)"
      >
        {{ LABELS[tab] || tab }}
        <span class="err-dot" aria-hidden="true"></span>
      </button>
    </div>

    <SchemaForm
      v-show="activeTab !== 'access'"
      :active-section="activeTab"
      @update:error-sections="errorSections = $event"
      @activate="activate"
    />
    <AccessTab v-show="activeTab === 'access'" />
  </div>
</template>

<style scoped>
.settings-tabs {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
  border-bottom: 1px solid var(--border-soft);
  margin-bottom: var(--sp-4);
}
.settings-tab {
  position: relative;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--text-2);
  padding: var(--sp-2) var(--sp-3);
  cursor: pointer;
  font: inherit;
}
.settings-tab:hover {
  color: var(--text);
}
.settings-tab.active {
  color: var(--action);
  border-bottom-color: var(--action);
  font-weight: 600;
}
.settings-tab .err-dot {
  display: none;
  position: absolute;
  top: 5px;
  right: 0;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--danger);
}
.settings-tab.has-error .err-dot {
  display: block;
}
</style>
