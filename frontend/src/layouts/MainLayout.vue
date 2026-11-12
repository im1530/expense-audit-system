<template>
  <el-container class="layout">
    <!-- 侧边栏：菜单按角色过滤 -->
    <el-aside width="210px" class="aside">
      <div class="logo">
        <span class="logo-icon">AI</span>
        <span class="logo-text">报销审核系统</span>
      </div>
      <el-menu :default-active="activeMenu" router class="menu">
        <el-menu-item index="/dashboard">
          <el-icon><HomeFilled /></el-icon><span>工作台</span>
        </el-menu-item>
        <el-menu-item index="/expenses">
          <el-icon><Document /></el-icon><span>我的报销</span>
        </el-menu-item>
        <el-menu-item index="/expenses/new">
          <el-icon><Plus /></el-icon><span>新建报销</span>
        </el-menu-item>
        <el-menu-item v-if="store.canApprove" index="/approvals">
          <el-icon><Checked /></el-icon><span>审批中心</span>
        </el-menu-item>
        <el-menu-item v-if="store.canViewReports" index="/reports">
          <el-icon><DataLine /></el-icon><span>统计报表</span>
        </el-menu-item>

        <!-- 管理端：仅 admin 可见 -->
        <el-sub-menu v-if="store.isAdmin" index="admin">
          <template #title>
            <el-icon><Setting /></el-icon><span>系统管理</span>
          </template>
          <el-menu-item index="/admin/users">用户管理</el-menu-item>
          <el-menu-item index="/admin/departments">部门管理</el-menu-item>
          <el-menu-item index="/admin/rules">规则管理</el-menu-item>
          <el-menu-item index="/admin/flows">审批流管理</el-menu-item>
        </el-sub-menu>
      </el-menu>
    </el-aside>

    <el-container>
      <!-- 顶栏：面包屑 + 用户菜单 -->
      <el-header class="header">
        <div class="header-left">
          <el-breadcrumb separator="/">
            <el-breadcrumb-item>{{ route.meta.title || '报销系统' }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="header-right">
          <el-tag size="small" type="info" effect="plain">{{ roleLabel }}</el-tag>
          <el-dropdown @command="onCommand">
            <span class="user-name">
              {{ store.displayName }}
              <el-icon><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="password">修改密码</el-dropdown-item>
                <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>

    <!-- 首登强制改密 -->
    <el-dialog v-model="passwordVisible" title="请先修改初始密码" width="420px" :close-on-click-modal="false">
      <el-form :model="passwordForm" label-width="90px">
        <el-form-item label="旧密码">
          <el-input v-model="passwordForm.oldPassword" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="passwordForm.newPassword" type="password" show-password placeholder="6~128 位，不能与旧密码相同" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="onLogout">退出</el-button>
        <el-button type="primary" :loading="passwordLoading" @click="onChangePassword">确认修改</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, Checked, DataLine, Document, HomeFilled, Plus, Setting } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const store = useUserStore()

// 当前高亮菜单（详情页归到"我的报销"）
const activeMenu = computed(() => {
  if (route.path.startsWith('/expenses/') && route.path !== '/expenses/new') return '/expenses'
  return route.path
})

// 角色中文名
const ROLE_LABELS: Record<string, string> = {
  admin: '系统管理员',
  boss: '总经理',
  finance: '财务',
  manager: '部门经理',
  employee: '员工',
}
const roleLabel = computed(() => ROLE_LABELS[store.role] || '未知角色')

// 改密弹窗状态
const passwordVisible = ref(false)
const passwordLoading = ref(false)
const passwordForm = reactive({ oldPassword: '', newPassword: '' })

// 首登强制改密：进入布局就弹窗
watch(
  () => store.mustChangePassword,
  (value) => {
    if (value) passwordVisible.value = true
  },
  { immediate: true },
)

/**
 * 提交改密
 */
async function onChangePassword() {
  if (!passwordForm.oldPassword || !passwordForm.newPassword) {
    ElMessage.warning('请填写旧密码与新密码')
    return
  }
  passwordLoading.value = true
  try {
    await store.changePassword(passwordForm.oldPassword, passwordForm.newPassword)
    ElMessage.success('密码已修改')
    passwordVisible.value = false
    passwordForm.oldPassword = ''
    passwordForm.newPassword = ''
  } finally {
    passwordLoading.value = false
  }
}

/**
 * 顶栏下拉命令
 * @param command 命令名
 */
function onCommand(command: string) {
  if (command === 'logout') {
    onLogout()
  } else if (command === 'password') {
    passwordVisible.value = true
  }
}

/**
 * 退出登录
 */
function onLogout() {
  store.logout()
  router.replace('/login')
}
</script>

<style scoped>
.layout {
  height: 100vh;
}

.aside {
  background: #1f2d3d;
  color: #fff;
  display: flex;
  flex-direction: column;
}

.logo {
  height: 56px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.logo-icon {
  width: 26px;
  height: 26px;
  border-radius: 6px;
  background: #409eff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
}

.logo-text {
  font-size: 14px;
  font-weight: 600;
}

.menu {
  border-right: none;
  background: transparent;
  flex: 1;
}

.menu :deep(.el-menu-item) {
  color: #c0c4cc;
}

.menu :deep(.el-menu-item.is-active) {
  color: #fff;
  background: #409eff;
}

.header {
  height: 56px;
  background: #fff;
  border-bottom: 1px solid #ebeef5;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.user-name {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 14px;
}

.main {
  padding: 16px;
  overflow: auto;
}
</style>
