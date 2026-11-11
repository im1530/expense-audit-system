<template>
  <div class="page-card">
    <el-tabs v-model="tab" @tab-change="onTabChange">
      <el-tab-pane label="待我审批" name="pending" />
      <el-tab-pane label="我已审批" name="processed" />
    </el-tabs>

    <el-table :data="rows" v-loading="loading" border stripe>
      <el-table-column prop="expense_no" label="单号" width="200" />
      <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
      <el-table-column label="金额" width="130" align="right">
        <template #default="{ row }">
          <span class="amount">{{ formatMoney(row.total_amount) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="规则风险" width="150">
        <template #default="{ row }">
          <el-tag v-if="row.rule_risk_level && row.rule_risk_level !== 'ok'" size="small" :type="RULE_LEVEL[row.rule_risk_level]?.type">
            {{ RULE_LEVEL[row.rule_risk_level]?.label }}（{{ row.rule_risk_count }}）
          </el-tag>
          <span v-else class="muted">无</span>
        </template>
      </el-table-column>
      <el-table-column label="当前层级" width="100">
        <template #default="{ row }">第 {{ row.current_level }} 级</template>
      </el-table-column>
      <el-table-column label="创建时间" width="160">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="router.push(`/expenses/${row.id}`)">详情</el-button>
          <template v-if="tab === 'pending'">
            <el-button link type="success" @click="openAction(row, 'approved')">通过</el-button>
            <el-button link type="danger" @click="openAction(row, 'rejected')">驳回</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="total, prev, pager, next"
        @current-change="load"
      />
    </div>

    <!-- 审批意见弹窗 -->
    <el-dialog v-model="dialogVisible" :title="action === 'approved' ? '审批通过' : '审批驳回'" width="460px">
      <p class="muted">{{ current?.title }}</p>
      <el-input v-model="comment" type="textarea" :rows="3" placeholder="审批意见（选填）" />
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button :type="action === 'approved' ? 'success' : 'danger'" :loading="submitting" @click="onConfirm">
          确认{{ action === 'approved' ? '通过' : '驳回' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { approve, listPending, listProcessed } from '@/api/approval'
import { RULE_LEVEL, formatMoney, formatTime } from '@/utils/helpers'

const router = useRouter()

const tab = ref<'pending' | 'processed'>('pending')
const rows = ref<any[]>([])
const loading = ref(false)
const page = ref(1)
const pageSize = 10
const total = ref(0)

// 审批弹窗
const dialogVisible = ref(false)
const submitting = ref(false)
const current = ref<any>(null)
const action = ref<'approved' | 'rejected'>('approved')
const comment = ref('')

/**
 * 拉取当前 tab 的列表
 */
async function load() {
  loading.value = true
  try {
    const api = tab.value === 'pending' ? listPending : listProcessed
    const data: any = await api({ page: page.value, page_size: pageSize })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

/**
 * 切换 tab
 */
function onTabChange() {
  page.value = 1
  load()
}

/**
 * 打开审批弹窗
 * @param row 单据
 * @param next 审批结果
 */
function openAction(row: any, next: 'approved' | 'rejected') {
  current.value = row
  action.value = next
  comment.value = ''
  dialogVisible.value = true
}

/**
 * 提交审批
 */
async function onConfirm() {
  if (!current.value) return
  submitting.value = true
  try {
    await approve(current.value.id, action.value, comment.value || undefined)
    ElMessage.success('审批已提交')
    dialogVisible.value = false
    load()
  } finally {
    submitting.value = false
  }
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
