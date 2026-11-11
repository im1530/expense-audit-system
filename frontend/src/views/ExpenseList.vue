<template>
  <div class="page-card">
    <!-- 筛选区 -->
    <div class="filter-bar">
      <el-select v-model="query.status" placeholder="全部状态" clearable style="width: 140px" @change="reload">
        <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
      </el-select>
      <el-select v-model="query.expense_type" placeholder="全部费用大类" clearable style="width: 150px" @change="reload">
        <el-option v-for="(label, value) in EXPENSE_TYPE" :key="value" :label="label" :value="value" />
      </el-select>
      <el-date-picker
        v-model="dateRange"
        type="daterange"
        value-format="YYYY-MM-DD"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
        style="width: 260px"
        @change="reload"
      />
      <el-button type="primary" @click="reload">查询</el-button>
      <el-button @click="onReset">重置</el-button>
      <div style="flex: 1"></div>
      <el-button type="primary" @click="router.push('/expenses/new')">新建报销</el-button>
    </div>

    <!-- 列表 -->
    <el-table :data="rows" v-loading="loading" border stripe>
      <el-table-column prop="expense_no" label="单号" width="200" />
      <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
      <el-table-column label="金额" width="130" align="right">
        <template #default="{ row }">
          <span class="amount">{{ formatMoney(row.total_amount) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="EXPENSE_STATUS[row.status]?.type">
            {{ EXPENSE_STATUS[row.status]?.label || row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="AI 风险" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.risk_level" size="small" :type="RISK_LEVEL[row.risk_level]?.type">
            {{ RISK_LEVEL[row.risk_level]?.label }}
          </el-tag>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="160">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="router.push(`/expenses/${row.id}`)">详情</el-button>
          <el-button v-if="row.status === 'draft'" link type="success" @click="onSubmit(row)">提交</el-button>
          <el-button v-if="row.status === 'pending'" link type="warning" @click="onRecall(row)">撤回</el-button>
          <el-button v-if="['draft', 'rejected'].includes(row.status)" link type="danger" @click="onCancel(row)">
            撤销
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
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
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { cancelExpense, listMyExpenses, recallExpense, submitExpense } from '@/api/expense'
import { EXPENSE_STATUS, EXPENSE_TYPE, RISK_LEVEL, formatMoney, formatTime } from '@/utils/helpers'

const router = useRouter()

const loading = ref(false)
const rows = ref<any[]>([])
const total = ref(0)
const dateRange = ref<string[]>([])

// 查询条件
const query = reactive({
  page: 1,
  page_size: 10,
  status: '',
  expense_type: '',
  start_date: '',
  end_date: '',
})

// 状态选项（与后端枚举一致）
const statusOptions = Object.entries(EXPENSE_STATUS).map(([value, meta]) => ({ value, label: meta.label }))

/**
 * 拉取列表
 */
async function load() {
  loading.value = true
  try {
    query.start_date = dateRange.value?.[0] || ''
    query.end_date = dateRange.value?.[1] || ''
    const data: any = await listMyExpenses({ ...query })
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
 * 重置筛选
 */
function onReset() {
  query.status = ''
  query.expense_type = ''
  dateRange.value = []
  reload()
}

/**
 * 提交送审
 * @param row 单据
 */
async function onSubmit(row: any) {
  await ElMessageBox.confirm(`确认提交「${row.title}」送审？提交后将自动触发 AI 预审。`, '提交确认', { type: 'warning' })
  await submitExpense(row.id)
  ElMessage.success('已提交')
  load()
}

/**
 * 撤回提交
 * @param row 单据
 */
async function onRecall(row: any) {
  await ElMessageBox.confirm('撤回后单据回到草稿状态，可修改后重新提交。', '撤回确认', { type: 'warning' })
  await recallExpense(row.id)
  ElMessage.success('已撤回')
  load()
}

/**
 * 撤销单据
 * @param row 单据
 */
async function onCancel(row: any) {
  await ElMessageBox.confirm('撤销后单据不可再提交，请确认。', '撤销确认', { type: 'warning' })
  await cancelExpense(row.id)
  ElMessage.success('已撤销')
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
