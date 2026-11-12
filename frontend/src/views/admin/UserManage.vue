<template>
  <div class="page-card">
    <!-- 筛选 -->
    <div class="filter-bar">
      <el-input v-model="query.keyword" placeholder="用户名/姓名/手机号" clearable style="width: 200px" @keyup.enter="reload" />
      <el-select v-model="query.role" placeholder="全部角色" clearable style="width: 140px" @change="reload">
        <el-option v-for="r in roles" :key="r.value" :label="r.label" :value="r.value" />
      </el-select>
      <el-select v-model="query.department_id" placeholder="全部部门" clearable style="width: 160px" @change="reload">
        <el-option v-for="d in departments" :key="d.id" :label="d.name" :value="d.id" />
      </el-select>
      <el-select v-model="query.is_active" placeholder="全部状态" clearable style="width: 120px" @change="reload">
        <el-option label="启用" :value="true" />
        <el-option label="停用" :value="false" />
      </el-select>
      <el-button type="primary" @click="reload">查询</el-button>
      <div style="flex: 1"></div>
      <el-button type="primary" @click="openCreate">新建用户</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" border stripe>
      <el-table-column prop="username" label="用户名" width="140" />
      <el-table-column prop="full_name" label="姓名" width="120" />
      <el-table-column prop="email" label="邮箱" min-width="180" show-overflow-tooltip />
      <el-table-column label="角色" width="110">
        <template #default="{ row }">
          <el-tag size="small">{{ roleLabel(row.role) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="部门" width="140">
        <template #default="{ row }">{{ deptName(row.department_id) }}</template>
      </el-table-column>
      <el-table-column prop="phone" label="手机号" width="130" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.is_active ? 'success' : 'info'">{{ row.is_active ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="warning" @click="openReset(row)">重置密码</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="query.page"
        v-model:page-size="query.page_size"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next"
        @current-change="load"
        @size-change="reload"
      />
    </div>

    <!-- 新建/编辑 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑用户' : '新建用户'" width="520px">
      <el-form ref="formRef" :model="form" label-width="90px">
        <el-form-item label="用户名">
          <el-input v-model="form.username" :disabled="editing" placeholder="登录名" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="form.email" />
        </el-form-item>
        <el-form-item v-if="!editing" label="初始密码">
          <el-input v-model="form.password" type="password" show-password placeholder="6~128 位，首次登录需改密" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.full_name" />
        </el-form-item>
        <el-form-item label="手机号">
          <el-input v-model="form.phone" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role">
            <el-option v-for="r in roles" :key="r.value" :label="r.label" :value="r.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="部门">
          <el-select v-model="form.department_id" clearable>
            <el-option v-for="d in departments" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="职位">
          <el-input v-model="form.position" />
        </el-form-item>
        <el-form-item v-if="editing" label="启用">
          <el-switch v-model="form.is_active" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码 -->
    <el-dialog v-model="resetVisible" title="重置密码" width="420px">
      <p class="muted">{{ current?.username }}：重置后该用户下次登录必须改密</p>
      <el-input v-model="resetPassword" type="password" show-password placeholder="新密码 6~128 位" />
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onReset">确认重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createAdminUser,
  listAdminUsers,
  listDepartments,
  listRoles,
  resetUserPassword,
  updateAdminUser,
} from '@/api/admin'

const loading = ref(false)
const saving = ref(false)
const rows = ref<any[]>([])
const total = ref(0)
const roles = ref<any[]>([])
const departments = ref<any[]>([])

// 查询条件
const query = reactive({
  page: 1,
  page_size: 10,
  keyword: '',
  role: '',
  department_id: undefined as number | undefined,
  is_active: undefined as boolean | undefined,
})

// 弹窗状态
const dialogVisible = ref(false)
const editing = ref(false)
const currentId = ref<number>()
const formRef = ref()
const form = reactive<any>({})

// 重置密码弹窗
const resetVisible = ref(false)
const resetPassword = ref('')
const current = ref<any>(null)

/**
 * 角色中文名
 * @param value 角色值
 */
function roleLabel(value: string) {
  return roles.value.find((r) => r.value === value)?.label || value
}

/**
 * 部门名称
 * @param id 部门ID
 */
function deptName(id?: number) {
  if (!id) return '-'
  return departments.value.find((d) => d.id === id)?.name || String(id)
}

/**
 * 拉取用户列表
 */
async function load() {
  loading.value = true
  try {
    const data: any = await listAdminUsers({ ...query })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

/**
 * 条件变化后回到第一页
 */
function reload() {
  query.page = 1
  load()
}

/**
 * 打开新建弹窗
 */
function openCreate() {
  editing.value = false
  currentId.value = undefined
  Object.keys(form).forEach((key) => delete form[key])
  Object.assign(form, {
    username: '',
    email: '',
    password: '',
    full_name: '',
    phone: '',
    role: 'employee',
    department_id: undefined,
    position: '',
  })
  dialogVisible.value = true
}

/**
 * 打开编辑弹窗
 * @param row 用户
 */
function openEdit(row: any) {
  editing.value = true
  currentId.value = row.id
  Object.assign(form, {
    username: row.username,
    email: row.email,
    full_name: row.full_name,
    phone: row.phone,
    role: row.role,
    department_id: row.department_id,
    position: row.position,
    is_active: row.is_active,
  })
  dialogVisible.value = true
}

/**
 * 保存（新建或修改）
 */
async function onSave() {
  saving.value = true
  try {
    if (editing.value && currentId.value) {
      await updateAdminUser(currentId.value, {
        email: form.email,
        full_name: form.full_name,
        phone: form.phone,
        role: form.role,
        department_id: form.department_id ?? null,
        position: form.position || null,
        is_active: form.is_active,
      })
    } else {
      await createAdminUser({
        username: form.username,
        email: form.email,
        password: form.password,
        full_name: form.full_name,
        phone: form.phone,
        role: form.role,
        department_id: form.department_id ?? null,
        position: form.position || null,
      })
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    load()
  } finally {
    saving.value = false
  }
}

/**
 * 打开重置密码弹窗
 * @param row 用户
 */
function openReset(row: any) {
  current.value = row
  resetPassword.value = ''
  resetVisible.value = true
}

/**
 * 提交重置密码
 */
async function onReset() {
  if (!current.value) return
  if (resetPassword.value.length < 6) {
    ElMessage.warning('密码至少 6 位')
    return
  }
  saving.value = true
  try {
    await resetUserPassword(current.value.id, resetPassword.value)
    ElMessage.success('密码已重置，该用户下次登录需改密')
    resetVisible.value = false
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  const [roleList, deptList]: any[] = await Promise.all([listRoles(), listDepartments()])
  roles.value = roleList || []
  departments.value = deptList || []
  load()
})
</script>

<style scoped>
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}
</style>
