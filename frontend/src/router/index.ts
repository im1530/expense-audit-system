import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/stores/user'

// 路由表：登录页独立，其余页面挂在 MainLayout 下
const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/Login.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/Dashboard.vue'),
        meta: { title: '工作台' },
      },
      {
        path: 'expenses',
        name: 'expenses',
        component: () => import('@/views/ExpenseList.vue'),
        meta: { title: '我的报销' },
      },
      {
        path: 'expenses/new',
        name: 'expense-new',
        component: () => import('@/views/ExpenseSubmit.vue'),
        meta: { title: '新建报销' },
      },
      {
        path: 'expenses/:id',
        name: 'expense-detail',
        component: () => import('@/views/ExpenseDetail.vue'),
        meta: { title: '报销详情' },
      },
      {
        path: 'approvals',
        name: 'approvals',
        component: () => import('@/views/ApprovalCenter.vue'),
        meta: { title: '审批中心', requiresApprove: true },
      },
      {
        path: 'reports',
        name: 'reports',
        component: () => import('@/views/Reports.vue'),
        meta: { title: '统计报表', requiresReports: true },
      },
      {
        path: 'trace/:id',
        name: 'trace',
        component: () => import('@/views/TraceView.vue'),
        meta: { title: 'AI 预审轨迹' },
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 全局前置守卫：未登录跳登录页；无权限的菜单直接回工作台
router.beforeEach(async (to) => {
  const store = useUserStore()

  // 1.公开页面直接放行
  if (to.meta.public) {
    return true
  }

  // 2.未登录：跳登录页并记住来源
  if (!store.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  // 3.已登录但用户资料尚未加载（刷新页面场景）：先拉一次
  if (!store.user) {
    try {
      await store.fetchMe()
    } catch {
      store.logout()
      return { path: '/login', query: { redirect: to.fullPath } }
    }
  }

  // 4.强制改密：除登录页外一律先去改密（由布局里的弹窗处理）
  if (store.mustChangePassword && to.path !== '/login') {
    return true
  }

  // 5.角色权限校验
  if (to.meta.requiresApprove && !store.canApprove) {
    return { path: '/dashboard' }
  }
  if (to.meta.requiresReports && !store.canViewReports) {
    return { path: '/dashboard' }
  }
  return true
})

export default router
