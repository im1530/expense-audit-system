<template>
  <div class="page-card">
    <el-card shadow="never" v-loading="loading">
      <template #header>
        <div class="card-header">
          <span>待支付 / 全公司报销</span>
          <div class="toolbar">
            <el-select v-model="status" style="width: 160px" @change="reload">
              <el-option label="待支付（已通过）" value="approved" />
              <el-option label="已支付" value="paid" />
              <el-option label="全部状态" value="" />
            </el-select>
            <el-button @click="load">刷新</el-button>
          </div>
        </div>
      </template>

      <el-table :data="rows" border size="small">
        <el-table-column prop="expense_no" label="单号" min-width="180" />
        <el-table-column prop="title" label="标题" min-width="220" />
        <el-table-column label="金额" width="120" align="right">
          <template #default="{ row }">{{ formatMoney(row.total_amount) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="EXPENSE_STATUS[row.status]?.type">
              {{ EXPENSE_STATUS[row.status]?.label || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="AI 风险" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.risk_level" size="small" :type="RISK_LEVEL[row.risk_level]?.type">
              {{ RISK_LEVEL[row.risk_level]?.label }} {{ row.risk_score ?? '' }}
            </el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="提交时间" width="170">
          <template #default="{ row }">{{ formatTime(row.submitted_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/expenses/${row.id}`)">详情</el-button>
            <el-button
              v-if="row.status === 'approved'"
              link
              type="success"
              @click="onPay(row)"
            >
              确认支付
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="reload"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listCompanyExpenses, payExpense } from '@/api/expense'
import { EXPENSE_STATUS, RISK_LEVEL, formatMoney, formatTime } from '@/utils/helpers'

const router = useRouter()
const loading = ref(false)
const rows = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const status = ref('approved')

/**
 * 拉取全公司报销单（财务支付视图）
 */
async function load() {
  loading.value = true
  try {
    const params: Record<string, any> = { page: page.value, page_size: pageSize.value }
    if (status.value) params.status = status.value
    const data: any = await listCompanyExpenses(params)
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

/**
 * 筛选变化后回到第一页
 */
function reload() {
  page.value = 1
  load()
}

/**
 * 财务确认支付
 * @param row 报销单
 */
async function onPay(row: any) {
  await ElMessageBox.confirm(
    `确认支付「${row.title}」${formatMoney(row.total_amount)}？支付后状态不可回退。`,
    '支付确认',
    { type: 'warning' },
  )
  await payExpense(row.id)
  ElMessage.success('已确认支付')
  load()
}

onMounted(load)
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.toolbar {
  display: flex;
  gap: 8px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}
</style>
