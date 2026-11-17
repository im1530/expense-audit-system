<template>
  <div v-loading="loading">
    <!-- 主单信息 -->
    <el-card shadow="never" class="page-card">
      <template #header>
        <div class="card-header">
          <div class="title-line">
            <span class="title">{{ expense.title }}</span>
            <el-tag :type="EXPENSE_STATUS[expense.status]?.type" size="small">
              {{ EXPENSE_STATUS[expense.status]?.label || expense.status }}
            </el-tag>
            <el-tag v-if="expense.risk_level" :type="RISK_LEVEL[expense.risk_level]?.type" size="small" effect="plain">
              AI {{ RISK_LEVEL[expense.risk_level]?.label }} {{ expense.risk_score || '' }}
            </el-tag>
          </div>
          <div class="actions">
            <el-button v-if="expense.status === 'draft' && isOwner" type="success" @click="onSubmit">提交送审</el-button>
            <el-button v-if="expense.status === 'pending' && isOwner" type="warning" @click="onRecall">撤回</el-button>
            <el-button v-if="expense.status === 'rejected' && isOwner" type="primary" @click="onResubmit">重新提交</el-button>
            <el-button v-if="canRegister" type="success" @click="openRegister">登记</el-button>
            <el-button v-if="canCancelRegistration" type="warning" @click="openCancelRegister">撤销登记</el-button>
            <el-button v-if="canConfirmReceived" type="success" @click="onConfirmReceived">确认已报销</el-button>
            <el-button v-if="canForceClose" type="danger" plain @click="onForceClose">强制关闭</el-button>
            <el-button v-if="canApproveNow" type="primary" @click="router.push('/approvals')">去审批</el-button>
          </div>
        </div>
      </template>

      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="单号">{{ expense.expense_no }}</el-descriptions-item>
        <el-descriptions-item label="总金额">
          <span class="amount">{{ formatMoney(expense.total_amount) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="当前层级">
          {{ expense.current_level ? `第 ${expense.current_level} 级` : '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ formatTime(expense.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="提交时间">{{ formatTime(expense.submitted_at) }}</el-descriptions-item>
        <el-descriptions-item label="支付时间">{{ formatTime(expense.paid_at) }}</el-descriptions-item>
        <el-descriptions-item label="说明" :span="3">{{ expense.description || '-' }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 明细 + 发票 -->
    <el-card shadow="never" class="page-card" style="margin-top: 16px">
      <template #header>
        <div class="card-header">
          <span>费用明细</span>
          <el-button v-if="canUpload" size="small" type="primary" plain @click="openUpload">上传发票</el-button>
        </div>
      </template>
      <el-table :data="expense.items || []" border size="small">
        <el-table-column label="类别" width="150">
          <template #default="{ row }">{{ categoryNames[row.category_id] || row.category_name || row.category_id }}</template>
        </el-table-column>
        <el-table-column prop="description" label="说明" min-width="180" />
        <el-table-column label="金额" width="120" align="right">
          <template #default="{ row }"><span class="amount">{{ formatMoney(row.amount) }}</span></template>
        </el-table-column>
        <el-table-column label="发生日期" width="120">
          <template #default="{ row }">{{ formatDate(row.expense_date) }}</template>
        </el-table-column>
        <el-table-column prop="invoice_no" label="发票号" width="150">
          <template #default="{ row }">{{ row.invoice_no || '-' }}</template>
        </el-table-column>
        <el-table-column label="发票" width="110">
          <template #default="{ row }">
            <el-button link type="primary" @click="loadInvoices(row)">查看附件</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 结算记录 -->
    <el-card v-if="settlements.length" shadow="never" class="page-card" style="margin-top: 16px">
      <template #header><span>结算记录</span></template>
      <el-table :data="settlements" border size="small">
        <el-table-column label="动作" width="110">
          <template #default="{ row }">{{ SETTLEMENT_ACTION[row.action] || row.action }}</template>
        </el-table-column>
        <el-table-column prop="operator_id" label="操作人ID" width="100" />
        <el-table-column prop="note" label="备注/原因" min-width="180" />
        <el-table-column prop="channel" label="渠道" width="90" />
        <el-table-column prop="transaction_no" label="流水号" width="180" />
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- AI 预审结论 -->
    <el-card shadow="never" class="page-card" style="margin-top: 16px">
      <template #header>
        <div class="card-header">
          <span>AI 预审结论</span>
          <div>
            <el-button size="small" @click="openTrace">查看工作流轨迹</el-button>
            <el-button size="small" type="primary" plain :loading="reviewing" @click="onTriggerReview">
              重新预审
            </el-button>
          </div>
        </div>
      </template>

      <el-empty v-if="!review" description="还没有预审结果" :image-size="70">
        <el-button type="primary" size="small" @click="onTriggerReview">立即预审</el-button>
      </el-empty>

      <div v-else>
        <el-alert
          :type="review.status === 'failed' ? 'error' : review.is_fallback ? 'warning' : 'info'"
          :closable="false"
          show-icon
          :title="reviewTitle"
        />
        <p class="summary">{{ review.summary }}</p>

        <el-table v-if="(review.findings || []).length" :data="review.findings" border size="small">
          <el-table-column prop="level" label="级别" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="RISK_LEVEL[row.level]?.type">{{ RISK_LEVEL[row.level]?.label || row.level }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="type" label="类型" width="100" />
          <el-table-column prop="message" label="提示" min-width="220" />
          <el-table-column prop="suggestion" label="建议" width="160" />
        </el-table>

        <div v-if="(review.policy_citations || []).length" class="citations">
          <div class="section-title">制度引用</div>
          <div v-for="item in review.policy_citations" :key="item.clause_id" class="citation">
            <el-tag size="small" type="info">{{ item.clause_id }}</el-tag>
            <span class="citation-text">「{{ item.quote }}」</span>
          </div>
        </div>
      </div>
    </el-card>

    <!-- 审批轨迹 -->
    <el-card shadow="never" class="page-card" style="margin-top: 16px">
      <template #header><span>审批轨迹</span></template>
      <el-timeline v-if="(history || []).length">
        <el-timeline-item
          v-for="item in history"
          :key="item.id"
          :timestamp="formatTime(item.created_at)"
          :type="APPROVAL_STATUS[item.status]?.type"
        >
          <b>{{ item.approver_name || '系统' }}</b>
          <span class="muted">（第 {{ item.level }} 级）</span>
          <el-tag size="small" :type="APPROVAL_STATUS[item.status]?.type" style="margin-left: 8px">
            {{ APPROVAL_STATUS[item.status]?.label || item.status }}
          </el-tag>
          <div class="muted">{{ item.comment || '无意见' }}</div>
        </el-timeline-item>
      </el-timeline>
      <el-empty v-else description="暂无审批记录" :image-size="60" />
    </el-card>

    <!-- 上传发票 -->
    <el-dialog v-model="uploadVisible" title="上传发票" width="460px">
      <el-form label-width="80px">
        <el-form-item label="明细">
          <el-select v-model="uploadItemId" placeholder="选择明细">
            <el-option
              v-for="item in expense.items || []"
              :key="item.id"
              :label="`${item.description}（${formatMoney(item.amount)}）`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="文件">
          <input type="file" accept="image/jpeg,image/png,application/pdf" @change="onFileChange" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="onUpload">上传</el-button>
      </template>
    </el-dialog>

    <!-- 附件列表：图片内嵌预览，PDF 新标签打开 -->
    <el-dialog v-model="invoiceVisible" title="附件" width="560px">
      <div v-if="invoices.length" class="invoice-grid">
        <div v-for="item in invoices" :key="item.id" class="invoice-item">
          <el-image
            v-if="isImage(item)"
            :src="item.url"
            :preview-src-list="imageUrls"
            :initial-index="imageUrls.indexOf(item.url)"
            fit="cover"
            class="invoice-thumb"
            preview-teleported
            hide-on-click-modal
          />
          <div v-else class="invoice-thumb pdf" @click="openInNewTab(item.url)">PDF</div>
          <div class="invoice-meta">
            <span class="muted" :title="item.file_name">{{ item.file_name }}</span>
            <el-button v-if="canUpload" link type="danger" size="small" @click="onDeleteInvoice(item)">删除</el-button>
          </div>
        </div>
      </div>
      <el-empty v-else description="该明细还没有附件" :image-size="60" />
    </el-dialog>

    <!-- 财务登记 -->
    <el-dialog v-model="registerVisible" title="登记报销单" width="520px">
      <el-form label-width="90px">
        <el-form-item label="备注">
          <el-input v-model="registerForm.note" type="textarea" :rows="3" maxlength="500" show-word-limit
            placeholder="选填；会随通知一起发给提交人" />
        </el-form-item>
        <el-form-item label="渠道">
          <el-select v-model="registerForm.channel" clearable placeholder="选填">
            <el-option label="银行转账" value="bank" />
            <el-option label="支付宝" value="alipay" />
            <el-option label="微信" value="wechat" />
            <el-option label="现金" value="cash" />
          </el-select>
        </el-form-item>
        <el-form-item label="流水号">
          <el-input v-model="registerForm.transaction_no" maxlength="64" placeholder="选填，唯一" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="registerVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onRegister">确认登记并通知</el-button>
      </template>
    </el-dialog>

    <!-- 撤销登记 -->
    <el-dialog v-model="cancelRegisterVisible" title="撤销登记" width="480px">
      <el-form label-width="90px">
        <el-form-item label="原因">
          <el-input v-model="cancelReason" type="textarea" :rows="3" maxlength="200" show-word-limit placeholder="选填" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cancelRegisterVisible = false">取消</el-button>
        <el-button type="warning" :loading="submitting" @click="onCancelRegister">确认撤销</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  cancelRegistration,
  confirmReceived,
  forceCloseExpense,
  getApprovalHistory,
  getExpense,
  listCategories,
  listInvoices as apiListInvoices,
  listSettlements,
  registerExpense,
  deleteInvoice,
  recallExpense,
  resubmitExpense,
  submitExpense,
  uploadInvoice,
} from '@/api/expense'
import { getReview, triggerReview } from '@/api/agent'
import { useUserStore } from '@/stores/user'
import {
  APPROVAL_STATUS,
  EXPENSE_STATUS,
  RISK_LEVEL,
  formatDate,
  formatMoney,
  formatTime,
} from '@/utils/helpers'

const route = useRoute()
const router = useRouter()
const store = useUserStore()

const expenseId = Number(route.params.id)
const loading = ref(false)
const expense = ref<any>({})
const review = ref<any>(null)
const history = ref<any[]>([])
const reviewing = ref(false)
const categoryNames = ref<Record<number, string>>({})
const settlements = ref<any[]>([])

// 结算弹窗
const registerVisible = ref(false)
const registerForm = ref({ note: '', channel: '', transaction_no: '' })
const cancelRegisterVisible = ref(false)
const cancelReason = ref('')
const submitting = ref(false)

// 上传弹窗
const uploadVisible = ref(false)
const uploadItemId = ref<number>()
const uploadFile = ref<File>()
const uploading = ref(false)

// 附件弹窗
const invoiceVisible = ref(false)
const invoices = ref<any[]>([])

const isFinance = computed(() => store.role === 'finance')
const isOwner = computed(() => expense.value.user_id === store.user?.id)
const canUpload = computed(() => isOwner.value && ['draft', 'rejected'].includes(expense.value.status))
const canApproveNow = computed(() => expense.value.status === 'pending' && store.canApprove)
const canRegister = computed(
  () => store.role === 'finance' && expense.value.status === 'approved' && !isOwner.value,
)
const canCancelRegistration = computed(
  () => store.role === 'finance' && expense.value.status === 'registered' && !isOwner.value,
)
const canConfirmReceived = computed(
  () => isOwner.value && expense.value.status === 'registered',
)
const canForceClose = computed(
  () => store.isAdmin && expense.value.status === 'registered',
)

const SETTLEMENT_ACTION: Record<string, string> = {
  register: '财务登记',
  cancel: '撤销登记',
  confirm: '确认领取',
  force_close: '强制关闭',
}

// 预审结论标题
const reviewTitle = computed(() => {
  if (!review.value) return ''
  if (review.value.status === 'failed') return '预审失败（已降级为规则兜底）'
  if (review.value.is_fallback) return 'AI 不可用，已降级为规则兜底'
  return `AI 建议：${DECISION_LABEL[review.value.decision] || review.value.decision || '已完成'}`
})

const DECISION_LABEL: Record<string, string> = {
  fast_track: '可快速审批',
  manual_review: '需人工复核',
  suggest_reject: '建议退回补正',
}

/**
 * 加载单据详情、预审结果与审批轨迹
 */
async function load() {
  loading.value = true
  try {
    expense.value = await getExpense(expenseId)
    try {
      const categories = (await listCategories()) || []
      categoryNames.value = Object.fromEntries(categories.map((item: any) => [item.id, item.name]))
    } catch {
      categoryNames.value = {} // 类别字典失败不影响详情主体
    }
    history.value = (await getApprovalHistory(expenseId)) || []
    try {
      settlements.value = (await listSettlements(expenseId)) || []
    } catch {
      settlements.value = [] // 结算流水失败不影响详情主体
    }
    try {
      review.value = await getReview(expenseId)
    } catch {
      review.value = null // 还没有预审记录
    }
  } finally {
    loading.value = false
  }
}

/**
 * 提交送审
 */
async function onSubmit() {
  await ElMessageBox.confirm('提交后自动触发 AI 预审，确认提交？', '提交确认', { type: 'warning' })
  await submitExpense(expenseId)
  ElMessage.success('已提交')
  load()
}

/**
 * 撤回
 */
async function onRecall() {
  await recallExpense(expenseId)
  ElMessage.success('已撤回')
  load()
}

/**
 * 驳回复提
 */
async function onResubmit() {
  await resubmitExpense(expenseId)
  ElMessage.success('已重新提交')
  load()
}

/**
 * 打开登记弹窗
 */
function openRegister() {
  registerForm.value = { note: '', channel: '', transaction_no: '' }
  registerVisible.value = true
}

/**
 * 财务登记
 */
async function onRegister() {
  submitting.value = true
  try {
    await registerExpense(expenseId, {
      note: registerForm.value.note || undefined,
      channel: registerForm.value.channel || undefined,
      transaction_no: registerForm.value.transaction_no || undefined,
    })
    ElMessage.success('已登记，已通知提交人领取')
    registerVisible.value = false
    load()
  } finally {
    submitting.value = false
  }
}

/**
 * 打开撤销登记弹窗
 */
function openCancelRegister() {
  cancelReason.value = ''
  cancelRegisterVisible.value = true
}

/**
 * 撤销登记
 */
async function onCancelRegister() {
  submitting.value = true
  try {
    await cancelRegistration(expenseId, { reason: cancelReason.value || undefined })
    ElMessage.success('已撤销登记')
    cancelRegisterVisible.value = false
    load()
  } finally {
    submitting.value = false
  }
}

/**
 * 提交人确认已收到报销款
 */
async function onConfirmReceived() {
  await ElMessageBox.confirm(
    '确认已经收到这笔报销款了吗？确认后状态将变为“已报销”，不可撤回。',
    '确认已报销',
    { type: 'warning', confirmButtonText: '确认已收到', cancelButtonText: '取消' },
  )
  await confirmReceived(expenseId)
  ElMessage.success('已确认领取')
  load()
}

/**
 * 管理员强制关闭
 */
async function onForceClose() {
  const { value } = await ElMessageBox.prompt('请输入强制关闭原因（必填）', '强制关闭', {
    inputPlaceholder: '如：员工离职，财务确认已打款',
    inputValidator: (input: string) => (input && input.trim() ? true : '原因不能为空'),
  })
  await forceCloseExpense(expenseId, { reason: String(value).trim() })
  ElMessage.success('已强制关闭')
  load()
}

/**
 * 触发 AI 预审并轮询结果
 */
async function onTriggerReview() {
  reviewing.value = true
  try {
    await triggerReview(expenseId, true)
    ElMessage.success('已触发预审')
    // 轮询最多 30 秒
    for (let i = 0; i < 15; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2000))
      const data: any = await getReview(expenseId)
      if (['success', 'fallback', 'failed'].includes(data?.status)) {
        review.value = data
        break
      }
    }
    await load()
  } finally {
    reviewing.value = false
  }
}

/**
 * 打开轨迹页
 */
function openTrace() {
  router.push(`/trace/${expenseId}`)
}

/**
 * 打开上传弹窗
 */
function openUpload() {
  uploadItemId.value = expense.value.items?.[0]?.id
  uploadVisible.value = true
}

/**
 * 选择文件
 * @param event input 事件
 */
function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  uploadFile.value = input.files?.[0]
}

/**
 * 上传发票
 */
async function onUpload() {
  if (!uploadItemId.value || !uploadFile.value) {
    ElMessage.warning('请选择明细与文件')
    return
  }
  uploading.value = true
  try {
    await uploadInvoice(uploadItemId.value, uploadFile.value)
    ElMessage.success('上传成功')
    uploadVisible.value = false
  } finally {
    uploading.value = false
  }
}

/**
 * 查看某条明细的附件
 * @param row 明细
 */
async function loadInvoices(row: any) {
  invoices.value = (await apiListInvoices(row.id)) || []
  invoiceVisible.value = true
}

// 图片类附件的地址列表（供大图预览左右切换）
const imageUrls = computed(() => invoices.value.filter(isImage).map((item) => item.url))

/**
 * 是否图片类型（后端返回真实 MIME，按文件头嗅探）
 * @param item 附件
 */
function isImage(item: any) {
  return String(item?.content_type || '').startsWith('image/')
}

/**
 * 新标签打开（PDF 用）
 * @param url 签名地址
 */
function openInNewTab(url: string) {
  window.open(url, '_blank', 'noopener')
}

/**
 * 删除附件（仅草稿/已驳回可删）
 * @param item 附件
 */
async function onDeleteInvoice(item: any) {
  await ElMessageBox.confirm(`确认删除附件「${item.file_name}」？`, '删除确认', { type: 'warning' })
  await deleteInvoice(item.id)
  invoices.value = invoices.value.filter((row) => row.id !== item.id)
  ElMessage.success('已删除')
}

onMounted(load)
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.title-line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.title {
  font-size: 16px;
  font-weight: 600;
}

.summary {
  margin: 10px 0;
  color: #303133;
}

.citations {
  margin-top: 12px;
}

.section-title {
  font-weight: 600;
  margin-bottom: 6px;
}

.citation {
  display: flex;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px dashed #ebeef5;
}

.citation-text {
  color: #606266;
  font-size: 13px;
}

/* 附件网格：保持简洁，一屏可看多张 */
.invoice-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}

.invoice-item {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  overflow: hidden;
  background: #fafafa;
}

.invoice-thumb {
  width: 100%;
  height: 110px;
  display: block;
  cursor: zoom-in;
}

.invoice-thumb.pdf {
  display: flex;
  align-items: center;
  justify-content: center;
  color: #909399;
  font-weight: 600;
  cursor: pointer;
}

.invoice-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 4px 8px;
  font-size: 12px;
}

.invoice-meta .muted {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
