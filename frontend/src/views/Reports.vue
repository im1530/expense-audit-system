<template>
  <div>
    <!-- 筛选条：保持简洁，只放日期区间与导出 -->
    <div class="page-card" style="margin-bottom: 16px">
      <div class="filter-bar" style="margin-bottom: 0">
        <span class="muted">统计区间</span>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          style="width: 260px"
          @change="load"
        />
        <el-button @click="onReset">重置</el-button>
        <div style="flex: 1"></div>
        <el-button type="primary" :loading="exporting" @click="onExport">导出 Excel</el-button>
      </div>
    </div>

    <!-- 总览 -->
    <el-row :gutter="16">
      <el-col :span="6">
        <el-card shadow="never" class="stat">
          <div class="label">单据总数</div>
          <div class="value">{{ overview.total_count ?? '-' }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="stat">
          <div class="label">金额合计</div>
          <div class="value">{{ formatMoney(overview.total_amount) }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="stat">
          <div class="label">待审批</div>
          <div class="value warning">{{ overview.pending_count ?? '-' }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="stat">
          <div class="label">待审批金额</div>
          <div class="value warning">{{ formatMoney(overview.pending_amount) }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 按月 -->
    <el-card shadow="never" class="page-card" style="margin-top: 16px">
      <template #header><span>按月统计</span></template>
      <el-table :data="monthly" size="small" border>
        <el-table-column prop="month" label="月份" width="120" />
        <el-table-column prop="expense_count" label="单据数" width="120" />
        <el-table-column label="总金额" width="150">
          <template #default="{ row }">{{ formatMoney(row.total_amount) }}</template>
        </el-table-column>
        <el-table-column label="已报销" width="150">
          <template #default="{ row }">{{ formatMoney(row.paid_amount) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 按部门 / 类别 -->
    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="12">
        <el-card shadow="never" class="page-card">
          <template #header><span>按部门统计</span></template>
          <el-table :data="departments" size="small" border>
            <el-table-column prop="department_name" label="部门" />
            <el-table-column prop="expense_count" label="单据数" width="90" />
            <el-table-column label="总金额" width="130">
              <template #default="{ row }">{{ formatMoney(row.total_amount) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never" class="page-card">
          <template #header><span>按费用类别统计</span></template>
          <el-table :data="categories" size="small" border>
            <el-table-column prop="category_name" label="类别" />
            <el-table-column prop="item_count" label="明细数" width="90" />
            <el-table-column label="总金额" width="130">
              <template #default="{ row }">{{ formatMoney(row.total_amount) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { exportReport, listCategories, listDepartments, listMonthly, listOverview } from '@/api/report'
import { formatMoney } from '@/utils/helpers'

const overview = ref<any>({})
const monthly = ref<any[]>([])
const departments = ref<any[]>([])
const categories = ref<any[]>([])
const dateRange = ref<string[]>([])
const exporting = ref(false)

/**
 * 组装日期筛选参数
 * @returns 后端需要的 start_date / end_date
 */
function dateParams() {
  return {
    start_date: dateRange.value?.[0] || undefined,
    end_date: dateRange.value?.[1] || undefined,
  }
}

/**
 * 加载四类报表
 */
async function load() {
  const params = dateParams()
  const [o, m, d, c]: any[] = await Promise.all([
    listOverview(params),
    listMonthly(params),
    listDepartments(params),
    listCategories(params),
  ])
  overview.value = o || {}
  monthly.value = m || []
  departments.value = d || []
  categories.value = c || []
}

/**
 * 重置筛选
 */
function onReset() {
  dateRange.value = []
  load()
}

/**
 * 导出 Excel：读 Content-Disposition 里的文件名，缺省时用日期兜底
 */
async function onExport() {
  exporting.value = true
  try {
    const response: any = await exportReport(dateParams())
    const disposition = response.headers?.['content-disposition'] || ''
    const stamp = new Date().toISOString().slice(0, 10)
    // 优先取 RFC 5987 的 filename*（中文名），拿不到再退回 ASCII 的 filename
    const utf8Match = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
    const asciiMatch = /filename="?([^";]+)"?/i.exec(disposition)
    const filename = utf8Match
      ? decodeURIComponent(utf8Match[1])
      : asciiMatch
        ? asciiMatch[1]
        : `报销报表_${stamp}.xlsx`

    const blob = response.data as Blob
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
    ElMessage.success('已导出')
  } finally {
    exporting.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.stat {
  text-align: center;
}

.label {
  color: #909399;
  font-size: 13px;
}

.value {
  font-size: 22px;
  font-weight: 700;
  margin-top: 6px;
}

.value.warning {
  color: #e6a23c;
}
</style>
