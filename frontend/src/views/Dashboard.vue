<template>
  <div>
    <!-- 顶部问候 + AI 授权提醒 -->
    <el-card shadow="never" class="page-card">
      <div class="welcome">
        <div>
          <h2>你好，{{ store.displayName }}</h2>
          <p class="muted">今天是 {{ today }}，欢迎使用 AI 报销审核系统</p>
        </div>
        <el-button type="primary" @click="router.push('/expenses/new')">新建报销</el-button>
      </div>
      <el-alert
        v-if="store.user && !store.user.ai_consent_at"
        type="warning"
        :closable="false"
        show-icon
        title="尚未授权 AI 处理发票影像"
        description="授权后系统才能识别发票内容；不授权也能提交，但凭证类风险会少一道校验。"
      >
        <template #default>
          <div class="consent-line">
            <span>授权 AI 处理发票影像，可提升凭证校验准确度</span>
            <el-button size="small" type="primary" link @click="onConsent">立即授权</el-button>
          </div>
        </template>
      </el-alert>
    </el-card>

    <!-- 统计卡片 -->
    <el-row :gutter="16" class="stats">
      <el-col :span="6" v-for="item in cards" :key="item.label">
        <el-card shadow="never" class="stat-card">
          <div class="stat-label">{{ item.label }}</div>
          <div class="stat-value" :class="item.color">{{ item.value }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 最近单据 -->
    <el-card shadow="never" class="page-card">
      <template #header>
        <div class="card-header">
          <span>最近报销单</span>
          <el-button link type="primary" @click="router.push('/expenses')">查看全部</el-button>
        </div>
      </template>
      <el-table :data="recent" v-loading="loading" size="small">
        <el-table-column prop="expense_no" label="单号" width="190" />
        <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
        <el-table-column label="金额" width="120" align="right">
          <template #default="{ row }">
            <span class="amount">{{ formatMoney(row.total_amount) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="EXPENSE_STATUS[row.status]?.type">{{ EXPENSE_STATUS[row.status]?.label || row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="提交时间" width="160">
          <template #default="{ row }">{{ formatTime(row.submitted_at || row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/expenses/${row.id}`)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { listMyExpenses } from '@/api/expense'
import { updateAiConsent } from '@/api/auth'
import { useUserStore } from '@/stores/user'
import { EXPENSE_STATUS, formatMoney, formatTime } from '@/utils/helpers'

const router = useRouter()
const store = useUserStore()

const loading = ref(false)
const recent = ref<any[]>([])
const counts = ref<Record<string, number>>({})

// 今天日期
const today = computed(() => new Date().toLocaleDateString('zh-CN'))

// 统计卡片
const cards = computed(() => [
  { label: '草稿', value: counts.value.draft || 0, color: 'text-info' },
  { label: '待审批', value: counts.value.pending || 0, color: 'text-warning' },
  { label: '已通过', value: counts.value.approved || 0, color: 'text-success' },
  { label: '已报销', value: counts.value.paid || 0, color: 'text-primary' },
])

/**
 * 拉取最近单据与各状态数量
 */
async function load() {
  loading.value = true
  try {
    const data: any = await listMyExpenses({ page: 1, page_size: 8 })
    recent.value = data.items || []
    counts.value = {}
    for (const row of recent.value) {
      counts.value[row.status] = (counts.value[row.status] || 0) + 1
    }
  } finally {
    loading.value = false
  }
}

/**
 * 授权 AI 处理影像
 */
async function onConsent() {
  await updateAiConsent(true)
  await store.fetchMe()
  ElMessage.success('已授权')
}

onMounted(load)
</script>

<style scoped>
.welcome {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.welcome h2 {
  margin: 0 0 4px;
  font-size: 20px;
}

.consent-line {
  margin-top: 6px;
}

.stats {
  margin: 16px 0;
}

.stat-card {
  text-align: center;
}

.stat-label {
  color: #909399;
  font-size: 13px;
}

.stat-value {
  font-size: 26px;
  font-weight: 700;
  margin-top: 6px;
}

.text-info { color: #909399; }
.text-warning { color: #e6a23c; }
.text-success { color: #67c23a; }
.text-primary { color: #409eff; }

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
</style>
