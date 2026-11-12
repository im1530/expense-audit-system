<template>
  <div class="page-card">
    <div class="filter-bar">
      <el-input v-model="query.keyword" placeholder="流程名称" clearable style="width: 200px" @keyup.enter="reload" />
      <el-select v-model="query.is_active" placeholder="全部状态" clearable style="width: 120px" @change="reload">
        <el-option label="启用" :value="true" />
        <el-option label="停用" :value="false" />
      </el-select>
      <el-button type="primary" @click="reload">查询</el-button>
      <div style="flex: 1"></div>
      <el-button type="primary" @click="openCreate">新建审批流</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" border stripe>
      <el-table-column prop="name" label="流程名称" width="180" />
      <el-table-column label="审批层级" min-width="260">
        <template #default="{ row }">
          <el-tag v-for="level in row.levels || []" :key="level.level" size="small" style="margin-right: 6px">
            {{ level.level }}. {{ roleLabel(level.role) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="说明" min-width="160" show-overflow-tooltip />
      <el-table-column label="默认" width="80">
        <template #default="{ row }">
          <el-tag v-if="row.is_default" size="small" type="success">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.is_active ? 'success' : 'info'">{{ row.is_active ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="onDelete(row)">删除</el-button>
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

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑审批流' : '新建审批流'" width="560px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="名称">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="审批层级">
          <div class="levels">
            <div v-for="(level, index) in form.levels" :key="index" class="level-row">
              <span class="level-index">{{ index + 1 }}</span>
              <el-select v-model="level.role" style="width: 200px">
                <el-option label="部门经理（manager）" value="manager" />
                <el-option label="财务（finance）" value="finance" />
                <el-option label="其他财务（finance_other）" value="finance_other" />
                <el-option label="老板（boss）" value="boss" />
              </el-select>
              <el-button link type="danger" @click="removeLevel(index)">移除</el-button>
            </div>
            <el-button size="small" plain @click="addLevel">添加一级</el-button>
          </div>
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="form.is_default" />
        </el-form-item>
        <el-form-item label="启用">
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
import { ElMessage, ElMessageBox } from 'element-plus'
import { createAdminFlow, deleteAdminFlow, listAdminFlows, updateAdminFlow } from '@/api/admin'

const loading = ref(false)
const saving = ref(false)
const rows = ref<any[]>([])
const total = ref(0)

const query = reactive({
  page: 1,
  page_size: 10,
  keyword: '',
  is_active: undefined as boolean | undefined,
})

const dialogVisible = ref(false)
const editing = ref(false)
const currentId = ref<number>()
const form = reactive<any>({
  name: '',
  description: '',
  levels: [{ level: 1, role: 'manager' }],
  is_default: false,
  is_active: true,
})

// 角色中文名
const ROLE_LABELS: Record<string, string> = {
  manager: '部门经理',
  finance: '财务',
  finance_other: '其他财务',
  boss: '老板',
}

/**
 * 角色中文名
 * @param value 角色值
 */
function roleLabel(value: string) {
  return ROLE_LABELS[value] || value
}

/**
 * 拉取审批流列表
 */
async function load() {
  loading.value = true
  try {
    const data: any = await listAdminFlows({ ...query })
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
 * 追加一级
 */
function addLevel() {
  form.levels.push({ level: form.levels.length + 1, role: 'finance' })
}

/**
 * 移除一级并重排编号
 * @param index 下标
 */
function removeLevel(index: number) {
  form.levels.splice(index, 1)
  form.levels.forEach((level: any, i: number) => {
    level.level = i + 1
  })
}

/**
 * 新建
 */
function openCreate() {
  editing.value = false
  currentId.value = undefined
  Object.assign(form, {
    name: '',
    description: '',
    levels: [{ level: 1, role: 'manager' }],
    is_default: false,
    is_active: true,
  })
  dialogVisible.value = true
}

/**
 * 编辑
 * @param row 审批流
 */
function openEdit(row: any) {
  editing.value = true
  currentId.value = row.id
  Object.assign(form, {
    name: row.name,
    description: row.description,
    levels: (row.levels || []).map((level: any) => ({ level: level.level, role: level.role })),
    is_default: !!row.is_default,
    is_active: row.is_active,
  })
  dialogVisible.value = true
}

/**
 * 保存
 */
async function onSave() {
  if (!form.name || form.levels.length === 0) {
    ElMessage.warning('请填写名称并至少配置一级审批')
    return
  }
  saving.value = true
  try {
    const payload = {
      name: form.name,
      description: form.description || null,
      levels: form.levels.map((level: any, index: number) => ({ level: index + 1, role: level.role })),
      is_default: form.is_default,
      is_active: form.is_active,
    }
    if (editing.value && currentId.value) {
      await updateAdminFlow(currentId.value, payload)
    } else {
      await createAdminFlow(payload)
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    load()
  } finally {
    saving.value = false
  }
}

/**
 * 删除
 * @param row 审批流
 */
async function onDelete(row: any) {
  await ElMessageBox.confirm(`确认删除审批流「${row.name}」？被规则引用的流程删除后可能导致提交失败。`, '删除确认', {
    type: 'warning',
  })
  await deleteAdminFlow(row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
</script>

<style scoped>
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}

.levels {
  width: 100%;
}

.level-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.level-index {
  width: 20px;
  text-align: center;
  color: #909399;
}
</style>
