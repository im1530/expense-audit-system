<template>
  <div class="page-card">
    <div class="filter-bar">
      <el-input v-model="query.keyword" placeholder="规则名称" clearable style="width: 200px" @keyup.enter="reload" />
      <el-select v-model="query.rule_type" placeholder="全部类型" clearable style="width: 200px" @change="reload">
        <el-option label="金额类（amount）" value="amount" />
        <el-option label="类别限额提醒" value="check_category_limit" />
        <el-option label="发票缺失提醒" value="check_invoice_missing" />
        <el-option label="重复报销提醒" value="check_duplicate" />
        <el-option label="跨期报销提醒" value="check_period_late" />
        <el-option label="弱凭证金额提醒" value="check_evidence_strength" />
      </el-select>
      <el-select v-model="query.is_active" placeholder="全部状态" clearable style="width: 120px" @change="reload">
        <el-option label="启用" :value="true" />
        <el-option label="停用" :value="false" />
      </el-select>
      <el-button type="primary" @click="reload">查询</el-button>
      <div style="flex: 1"></div>
      <el-button type="primary" @click="openCreate">新建规则</el-button>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="规则由系统规则引擎执行，改配置会影响提交时的预检与自动审批链指派，请谨慎修改"
      style="margin-bottom: 12px"
    />

    <el-table :data="rows" v-loading="loading" border stripe>
      <el-table-column prop="name" label="规则名称" min-width="180" show-overflow-tooltip />
      <el-table-column prop="rule_type" label="类型" width="190" />
      <el-table-column prop="action" label="引擎动作" width="130" />
      <el-table-column prop="severity" label="风险级别" width="110" />
      <el-table-column prop="condition" label="条件说明" min-width="200" show-overflow-tooltip />
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

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑规则' : '新建规则'" width="620px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="规则名称">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="规则类型">
          <el-select v-model="form.rule_type" @change="onRuleTypeChange">
            <el-option label="金额类（amount）" value="amount" />
            <el-option label="类别限额提醒" value="check_category_limit" />
            <el-option label="发票缺失提醒" value="check_invoice_missing" />
            <el-option label="重复报销提醒" value="check_duplicate" />
            <el-option label="跨期报销提醒" value="check_period_late" />
            <el-option label="弱凭证金额提醒" value="check_evidence_strength" />
          </el-select>
        </el-form-item>
        <el-form-item label="引擎动作">
          <el-select v-model="form.action">
            <el-option label="指派审批链（assign_chain）" value="assign_chain" />
            <el-option label="产出检查提示（check）" value="check" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.rule_type !== 'amount'" label="风险级别">
          <el-select v-model="form.severity">
            <el-option label="提示（warn）" value="warn" />
            <el-option label="需复核（review）" value="review" />
            <el-option label="阻止提交（block）" value="block" />
          </el-select>
        </el-form-item>
        <el-form-item label="条件说明">
          <el-input v-model="form.condition" type="textarea" :rows="2" placeholder="给人看的一句话说明" />
        </el-form-item>
        <el-form-item label="配置 JSON">
          <el-input v-model="configText" type="textarea" :rows="6" placeholder='例如 {"min_amount": 0, "max_amount": 999.99, "flow_id": 2}' />
          <div class="muted">必须是合法 JSON 对象；金额类规则需要 flow_id / min_amount / max_amount / submitter_roles</div>
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
import { createAdminRule, deleteAdminRule, listAdminRules, updateAdminRule } from '@/api/admin'

const loading = ref(false)
const saving = ref(false)
const rows = ref<any[]>([])
const total = ref(0)

const query = reactive({
  page: 1,
  page_size: 10,
  keyword: '',
  rule_type: '',
  is_active: undefined as boolean | undefined,
})

const dialogVisible = ref(false)
const editing = ref(false)
const currentId = ref<number>()
const configText = ref('{}')
const form = reactive<any>({
  name: '', rule_type: 'amount', action: 'assign_chain', severity: '',
  condition: '', is_active: true,
})

/**
 * 规则类型切换：自动设置引擎动作与默认风险级别
 */
function onRuleTypeChange(value: string) {
  if (value === 'amount') {
    form.action = 'assign_chain'
    form.severity = ''
  } else {
    form.action = 'check'
    form.severity = form.severity || 'warn'
  }
}

/**
 * 拉取规则列表
 */
async function load() {
  loading.value = true
  try {
    const data: any = await listAdminRules({ ...query })
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
  Object.assign(form, {
    name: '', rule_type: 'amount', action: 'assign_chain', severity: '',
    condition: '', is_active: true,
  })
  configText.value = '{}'
  dialogVisible.value = true
}

/**
 * 编辑
 * @param row 规则
 */
function openEdit(row: any) {
  editing.value = true
  currentId.value = row.id
  Object.assign(form, {
    name: row.name,
    rule_type: row.rule_type,
    action: row.action,
    severity: row.severity || (row.rule_type === 'amount' ? '' : 'warn'),
    condition: row.condition,
    is_active: row.is_active,
  })
  configText.value = JSON.stringify(row.config || {}, null, 2)
  dialogVisible.value = true
}

/**
 * 保存
 */
async function onSave() {
  let config: any
  try {
    config = JSON.parse(configText.value || '{}')
    if (typeof config !== 'object' || Array.isArray(config)) throw new Error('必须是对象')
  } catch (error: any) {
    ElMessage.error(`配置 JSON 不合法：${error.message}`)
    return
  }

  saving.value = true
  try {
    const payload = {
      name: form.name,
      rule_type: form.rule_type,
      action: form.action,
      severity: form.rule_type === 'amount' ? null : (form.severity || 'warn'),
      condition: form.condition,
      config,
      is_active: form.is_active,
    }
    if (editing.value && currentId.value) {
      await updateAdminRule(currentId.value, payload)
    } else {
      await createAdminRule(payload)
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    load()
  } finally {
    saving.value = false
  }
}

/**
 * 删除规则
 * @param row 规则
 */
async function onDelete(row: any) {
  await ElMessageBox.confirm(`确认删除规则「${row.name}」？删除后不再参与预检。`, '删除确认', { type: 'warning' })
  await deleteAdminRule(row.id)
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
</style>
