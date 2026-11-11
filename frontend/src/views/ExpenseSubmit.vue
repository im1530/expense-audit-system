<template>
  <div class="page-card">
    <el-steps :active="1" simple style="margin-bottom: 18px">
      <el-step title="填写报销单" />
      <el-step title="提交送审（自动触发 AI 预审）" />
    </el-steps>

    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-form-item label="报销标题" prop="title">
        <el-input v-model="form.title" maxlength="200" show-word-limit placeholder="例如：11 月深圳出差费用报销" />
      </el-form-item>
      <el-form-item label="报销说明">
        <el-input v-model="form.description" type="textarea" :rows="2" placeholder="简要说明本次报销事由" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="form.remark" placeholder="选填" />
      </el-form-item>
    </el-form>

    <!-- 明细列表 -->
    <div class="items-header">
      <span>费用明细</span>
      <el-button size="small" type="primary" plain @click="addItem">添加明细</el-button>
    </div>

    <el-table :data="form.items" border size="small">
      <el-table-column label="费用类别" width="200">
        <template #default="{ row }">
          <el-select v-model="row.category_id" placeholder="选择类别" filterable>
            <el-option v-for="c in categories" :key="c.id" :label="`${c.name}（限额 ${c.max_amount ?? '不限'}）`" :value="c.id" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="费用说明" min-width="180">
        <template #default="{ row }">
          <el-input v-model="row.description" placeholder="如：深圳北-广州南高铁票" />
        </template>
      </el-table-column>
      <el-table-column label="金额" width="140">
        <template #default="{ row }">
          <el-input-number v-model="row.amount" :min="0.01" :precision="2" :controls="false" style="width: 100%" />
        </template>
      </el-table-column>
      <el-table-column label="发生日期" width="160">
        <template #default="{ row }">
          <el-date-picker v-model="row.expense_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
        </template>
      </el-table-column>
      <el-table-column label="发票号" width="150">
        <template #default="{ row }">
          <el-input v-model="row.invoice_no" placeholder="选填" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="70">
        <template #default="{ $index }">
          <el-button link type="danger" @click="removeItem($index)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="summary">
      合计：<span class="amount">{{ formatMoney(totalAmount) }}</span>
    </div>

    <div class="actions">
      <el-button @click="router.back()">取消</el-button>
      <el-button :loading="saving" @click="onSaveDraft">保存草稿</el-button>
      <el-button type="primary" :loading="saving" @click="onSaveAndSubmit">保存并提交</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { createExpense, listCategories, submitExpense } from '@/api/expense'
import { formatMoney } from '@/utils/helpers'

const router = useRouter()
const formRef = ref<FormInstance>()
const saving = ref(false)
const categories = ref<any[]>([])

// 表单：主单 + 明细
const form = reactive({
  title: '',
  description: '',
  remark: '',
  items: [] as any[],
})

// 校验规则
const rules: FormRules = {
  title: [{ required: true, message: '请输入报销标题', trigger: 'blur' }],
}

// 合计金额
const totalAmount = computed(() => form.items.reduce((sum, item) => sum + Number(item.amount || 0), 0))

/**
 * 追加一条空明细
 */
function addItem() {
  form.items.push({
    category_id: categories.value[0]?.id,
    description: '',
    amount: 0,
    expense_date: new Date().toISOString().slice(0, 10),
    invoice_no: '',
  })
}

/**
 * 删除明细
 * @param index 下标
 */
function removeItem(index: number) {
  form.items.splice(index, 1)
}

/**
 * 组装提交载荷
 * @returns 后端 ExpenseCreate 结构
 */
function buildPayload() {
  return {
    title: form.title.trim(),
    description: form.description || null,
    remark: form.remark || null,
    // 明细去掉空发票号，避免后端把空串当有效发票号
    items: form.items.map((item) => ({
      category_id: item.category_id,
      description: item.description,
      amount: item.amount,
      expense_date: item.expense_date,
      invoice_no: item.invoice_no || null,
    })),
  }
}

/**
 * 基础校验（主单 + 至少一条明细）
 * @returns 是否通过
 */
async function validateAll(): Promise<boolean> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return false
  if (form.items.length === 0) {
    ElMessage.warning('至少需要一条费用明细')
    return false
  }
  for (const item of form.items) {
    if (!item.category_id || !item.description || !item.amount || !item.expense_date) {
      ElMessage.warning('请完整填写每条明细的类别、说明、金额与日期')
      return false
    }
  }
  return true
}

/**
 * 保存草稿
 */
async function onSaveDraft() {
  if (!(await validateAll())) return
  saving.value = true
  try {
    const created: any = await createExpense(buildPayload())
    ElMessage.success('草稿已保存')
    router.replace(`/expenses/${created.id}`)
  } finally {
    saving.value = false
  }
}

/**
 * 保存并提交送审
 */
async function onSaveAndSubmit() {
  if (!(await validateAll())) return
  saving.value = true
  try {
    const created: any = await createExpense(buildPayload())
    await submitExpense(created.id)
    ElMessage.success('已提交，正在触发 AI 预审')
    router.replace(`/expenses/${created.id}`)
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  const list: any = await listCategories()
  categories.value = list || []
  addItem()
})
</script>

<style scoped>
.items-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 16px 0 10px;
  font-weight: 600;
}

.summary {
  margin-top: 12px;
  text-align: right;
  font-size: 14px;
}

.actions {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
