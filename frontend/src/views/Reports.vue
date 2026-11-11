<template>
  <div>
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
        <el-table-column label="已支付" width="150">
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
import { listCategories, listDepartments, listMonthly, listOverview } from '@/api/report'
import { formatMoney } from '@/utils/helpers'

const overview = ref<any>({})
const monthly = ref<any[]>([])
const departments = ref<any[]>([])
const categories = ref<any[]>([])

/**
 * 加载四类报表
 */
async function load() {
  const [o, m, d, c]: any[] = await Promise.all([
    listOverview({}),
    listMonthly({}),
    listDepartments({}),
    listCategories({}),
  ])
  overview.value = o || {}
  monthly.value = m || []
  departments.value = d || []
  categories.value = c || []
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
