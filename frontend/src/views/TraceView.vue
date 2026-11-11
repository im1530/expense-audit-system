<template>
  <div v-loading="loading">
    <el-card shadow="never" class="page-card">
      <template #header>
        <div class="card-header">
          <span>AI 预审工作流轨迹</span>
          <el-button size="small" @click="router.back()">返回</el-button>
        </div>
      </template>

      <el-empty v-if="!trace" description="暂无轨迹（可能还没触发预审）" :image-size="80" />

      <div v-else>
        <!-- 概览 -->
        <el-descriptions :column="4" border size="small">
          <el-descriptions-item label="状态">{{ trace.status }}</el-descriptions-item>
          <el-descriptions-item label="引擎">{{ trace.engine || '-' }}</el-descriptions-item>
          <el-descriptions-item label="模型">{{ trace.model || '-' }}</el-descriptions-item>
          <el-descriptions-item label="Prompt 版本">{{ trace.prompt_version || '-' }}</el-descriptions-item>
          <el-descriptions-item label="决策">{{ trace.decision || '-' }}</el-descriptions-item>
          <el-descriptions-item label="风险">{{ trace.risk_level }} / {{ trace.risk_score }}</el-descriptions-item>
          <el-descriptions-item label="token">
            {{ trace.prompt_tokens }} + {{ trace.completion_tokens }}
          </el-descriptions-item>
          <el-descriptions-item label="耗时">
            {{ secondsBetween(trace.started_at, trace.finished_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="trace.decision_reason"
          :title="trace.decision_reason"
          type="info"
          :closable="false"
          show-icon
          style="margin: 12px 0"
        />

        <!-- 节点 -->
        <div class="section-title">节点耗时</div>
        <el-table :data="trace.steps || []" size="small" border>
          <el-table-column prop="node" label="节点" width="140" />
          <el-table-column prop="status" label="状态" width="100" />
          <el-table-column prop="latency_ms" label="耗时(ms)" width="100" align="right" />
          <el-table-column label="token" width="120" align="right">
            <template #default="{ row }">{{ row.prompt_tokens }} + {{ row.completion_tokens }}</template>
          </el-table-column>
          <el-table-column label="重试" width="80" align="center">
            <template #default="{ row }">{{ row.attempt }}</template>
          </el-table-column>
        </el-table>

        <!-- 引用 -->
        <div class="section-title" style="margin-top: 16px">制度引用（{{ (trace.citations || []).length }}）</div>
        <el-empty v-if="!(trace.citations || []).length" description="本次未引用制度条款" :image-size="50" />
        <div v-for="item in trace.citations || []" :key="item.clause_id" class="citation">
          <el-tag size="small" type="info">{{ item.clause_id }}</el-tag>
          <span>{{ item.quote }}</span>
        </div>

        <!-- 凭证结论 -->
        <div class="section-title" style="margin-top: 16px">凭证结论（{{ (trace.documents || []).length }}）</div>
        <el-table :data="trace.documents || []" size="small" border>
          <el-table-column prop="document_type" label="类型" width="150" />
          <el-table-column prop="evidence_level" label="凭证强度" width="110" />
          <el-table-column prop="amount" label="票面金额" width="120" />
          <el-table-column prop="identifier" label="号码" width="150" />
          <el-table-column prop="status" label="状态" width="100" />
          <el-table-column prop="error_message" label="备注" min-width="160" />
        </el-table>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getTrace } from '@/api/agent'

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const trace = ref<any>(null)

/**
 * 计算两个时间点之间的耗时（秒）
 * @param start 开始时间
 * @param end 结束时间
 * @returns 形如 3.2s
 */
function secondsBetween(start: string | null, end: string | null): string {
  if (!start || !end) return '-'
  const ms = new Date(end).getTime() - new Date(start).getTime()
  if (Number.isNaN(ms)) return '-'
  return `${(ms / 1000).toFixed(1)}s`
}

/**
 * 加载轨迹
 */
async function load() {
  loading.value = true
  try {
    trace.value = await getTrace(Number(route.params.id))
  } catch {
    trace.value = null
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.section-title {
  font-weight: 600;
  margin-bottom: 8px;
}

.citation {
  display: flex;
  gap: 8px;
  padding: 5px 0;
  border-bottom: 1px dashed #ebeef5;
  font-size: 13px;
  color: #606266;
}
</style>
