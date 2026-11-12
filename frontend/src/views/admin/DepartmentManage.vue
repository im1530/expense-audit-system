<template>
  <div class="page-card">
    <div class="filter-bar">
      <el-input v-model="query.keyword" placeholder="部门名称" clearable style="width: 200px" @keyup.enter="reload" />
      <el-select v-model="query.is_active" placeholder="全部状态" clearable style="width: 120px" @change="reload">
        <el-option label="启用" :value="true" />
        <el-option label="停用" :value="false" />
      </el-select>
      <el-button type="primary" @click="reload">查询</el-button>
      <div style="flex: 1"></div>
      <el-button type="primary" @click="openCreate">新建部门</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" border stripe>
      <el-table-column prop="name" label="部门名称" width="180" />
      <el-table-column prop="description" label="说明" min-width="220" show-overflow-tooltip />
      <el-table-column label="负责人" width="140">
        <template #default="{ row }">{{ userName(row.manager_id) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.is_active ? 'success' : 'info'">{{ row.is_active ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
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

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑部门' : '新建部门'" width="480px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="名称">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item v-if="editing" label="负责人">
          <el-select v-model="form.manager_id" clearable filterable>
            <el-option v-for="u in managers" :key="u.id" :label="`${u.full_name}（${u.username}）`" :value="u.id" />
          </el-select>
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
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { createAdminDepartment, listAdminDepartments, listAdminUsers, updateAdminDepartment } from '@/api/admin'

const loading = ref(false)
const saving = ref(false)
const rows = ref<any[]>([])
const total = ref(0)
const managers = ref<any[]>([])

const query = reactive({
  page: 1,
  page_size: 10,
  keyword: '',
  is_active: undefined as boolean | undefined,
})

const dialogVisible = ref(false)
const editing = ref(false)
const currentId = ref<number>()
const form = reactive<any>({ name: '', description: '', manager_id: undefined, is_active: true })

/**
 * 用户名显示
 * @param id 用户ID
 */
function userName(id?: number) {
  if (!id) return '未设置'
  return managers.value.find((u) => u.id === id)?.full_name || String(id)
}

/**
 * 拉取部门列表
 */
async function load() {
  loading.value = true
  try {
    const data: any = await listAdminDepartments({ ...query })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

/**
 * 条件变化后回第一页
 */
function reload() {
  query.page = 1
  load()
}

/**
 * 新建
 */
function openCreate() {
  editing.value = false
  currentId.value = undefined
  Object.assign(form, { name: '', description: '', manager_id: undefined, is_active: true })
  dialogVisible.value = true
}

/**
 * 编辑
 * @param row 部门
 */
function openEdit(row: any) {
  editing.value = true
  currentId.value = row.id
  Object.assign(form, {
    name: row.name,
    description: row.description,
    manager_id: row.manager_id,
    is_active: row.is_active,
  })
  dialogVisible.value = true
}

/**
 * 保存
 */
async function onSave() {
  if (!form.name) {
    ElMessage.warning('请填写部门名称')
    return
  }
  saving.value = true
  try {
    if (editing.value && currentId.value) {
      await updateAdminDepartment(currentId.value, {
        name: form.name,
        description: form.description || null,
        is_active: form.is_active,
        manager_id: form.manager_id ?? null,
      })
    } else {
      await createAdminDepartment({ name: form.name, description: form.description || null })
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    load()
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  // 负责人候选：只取经理角色
  const users: any = await listAdminUsers({ page: 1, page_size: 100, role: 'manager' })
  managers.value = users.items || []
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
