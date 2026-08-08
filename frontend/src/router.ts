// All final routes registered up front; later phases replace view files only.
// The global guard is load-bearing on the Vite dev server (which serves the
// shell for every path) and after client-side logout: it reads /api/auth/me
// (200 even unauthenticated / during first boot) and enforces the same gating
// the server middleware does for prod deep links.
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { fetchMe } from './stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'dashboard',
    component: () => import('./views/DashboardView.vue'),
    meta: { title: 'Dashboard' },
  },
  {
    path: '/pipeline',
    name: 'pipeline',
    component: () => import('./views/PipelineView.vue'),
    meta: { title: 'Pipeline' },
  },
  {
    path: '/review',
    name: 'review',
    component: () => import('./views/ReviewView.vue'),
    meta: { title: 'Review' },
  },
  {
    path: '/apply',
    name: 'apply',
    component: () => import('./views/ApplyView.vue'),
    meta: { title: 'Apply' },
  },
  {
    path: '/utilities',
    name: 'utilities',
    component: () => import('./views/UtilitiesView.vue'),
    meta: { title: 'Utilities' },
  },
  {
    path: '/maintenance',
    name: 'maintenance',
    component: () => import('./views/MaintenanceView.vue'),
    meta: { title: 'Maintenance' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('./views/SettingsView.vue'),
    meta: { title: 'Settings' },
  },
  {
    path: '/about',
    name: 'about',
    component: () => import('./views/AboutView.vue'),
    meta: { title: 'About' },
  },
  {
    path: '/jobs/:id',
    name: 'job',
    component: () => import('./views/JobView.vue'),
    meta: { title: 'Job' },
  },
  {
    path: '/setup',
    name: 'setup',
    component: () => import('./views/SetupView.vue'),
    meta: { title: 'Setup', bare: true },
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('./views/LoginView.vue'),
    meta: { title: 'Sign in', bare: true },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  let me
  try {
    me = await fetchMe()
  } catch {
    // /api/auth/me should always answer 200; on a hard network failure fall back
    // to the login screen rather than rendering chrome we cannot authorize.
    return to.name === 'login' ? true : { name: 'login', query: { next: to.fullPath } }
  }

  if (me.first_boot) {
    return to.name === 'setup' ? true : { name: 'setup' }
  }
  if (!me.authenticated) {
    return to.name === 'login' ? true : { name: 'login', query: { next: to.fullPath } }
  }
  // Authenticated: keep users out of the login shell. /setup stays reachable —
  // the old GET /setup page was a normal authenticated route too, so re-running
  // the wizard while signed in must keep working (its account step self-skips).
  if (to.name === 'login') {
    return { path: '/' }
  }
  return true
})

router.afterEach((to) => {
  const t = (to.meta.title as string | undefined) ?? 'Synopticon'
  document.title = `${t} · Synopticon`
})

export default router
