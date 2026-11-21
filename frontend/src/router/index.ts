import { createRouter, createWebHistory } from 'vue-router'

import FieldDiffView from '@/views/FieldDiffView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径必须排在动态模块路由之前，避免被当成业务模块 key。
    { path: '/field-diff', name: 'field-diff', component: FieldDiffView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
