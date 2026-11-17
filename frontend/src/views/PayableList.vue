<template>
  <div class="page-card">
    <el-card shadow="never" v-loading="loading">
      <template #header>
        <div class="card-header">
          <span>报销结算</span>
          <el-button @click="load">刷新</el-button>
        </div>
      </template>

      <el-tabs v-model="tab" @tab-change="reload">
        <el-tab-pane label="待登记" name="approved" />
        <el-tab-pane label="待领取" name="registered" />
        <el-tab-pane label="已报销" name="paid" />
      </el-tabs>

      <el-table :data="rows" border size="small">
        <el-table-column prop="expense_no" label="单号" min-width="190" />
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
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/expenses/${row.id}`)">详情</el-button>
            <el-button
              v-if="tab === 'approved'"
              link
              type="success"
              @click="openRegister(row)"
            >
              登记
            </el-button>
            <el-button
              v-if="tab === 'registered'"
              link
              type="warning"
              @click="openCancel(row)"
            >
              撤销登记
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

    <!-- 登记弹窗 -->
    <el-dialog v-model="registerVisible" title="登记报销单" width="520px">
      <el-form label-width="90px">
        <el-form-item label="报销单">
          <span>{{ current?.title }}（{{ formatMoney(current?.total_amount) }}）</span>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="registerForm.note"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="选填；会随通知一起发给提交人"
          />
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
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="登记后会通知提交人领取；提交人确认后状态才变为“已报销”。"
        />
      </el-form>
      <template #footer>
        <el-button @click="registerVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onRegister">确认登记并通知</el-button>
      </template>
    </el-dialog>

    <!-- 撤销登记弹窗 -->
    <el-dialog v-model="cancelVisible" title="撤销登记" width="480px">
      <el-form label-width="90px">
        <el-form-item label="报销单">
          <span>{{ current?.title }}（{{ formatMoney(current?.total_amount) }}）</span>
        </el-form-item>
        <el-form-item label="原因">
          <el-input v-model="cancelReason" type="textarea" :rows="3" maxlength="200" show-word-limit placeholder="选填" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cancelVisible = false">取消</el-button>
        <el-button type="warning" :loading="submitting" @click="onCancel">确认撤销</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { cancelRegistration, listCompanyExpenses, registerExpense } from '@/api/expense'
import { EXPENSE_STATUS, RISK_LEVEL, formatMoney, formatTime } from '@/utils/helpers'

const router = useRouter()
const loading = ref(false)
const submitting = ref(false)
const rows = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const tab = ref('approved')
const current = ref<any>(null)

const registerVisible = ref(false)
const registerForm = reactive({ note: '', channel: '', transaction_no: '' })
const cancelVisible = ref(false)
const cancelReason = ref('')

/**
 * 拉取对应状态的单据
 */
async function load() {
  loading.value = true
  try {
    const data: any = await listCompanyExpenses({
      page: page.value,
      page_size: pageSize.value,
      status: tab.value,
    })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

function reload() {
  page.value = 1
  load()
}

/**
 * 打开登记弹窗
 * @param row 报销单
 */
function openRegister(row: any) {
  current.value = row
  registerForm.note = ''
  registerForm.channel = ''
  registerForm.transaction_no = ''
  registerVisible.value = true
}

/**
 * 提交登记
 */
async function onRegister() {
  if (!current.value) return
  submitting.value = true
  try {
    await registerExpense(current.value.id, {
      note: registerForm.note || undefined,
      channel: registerForm.channel || undefined,
      transaction_no: registerForm.transaction_no || undefined,
    })
    ElMessage.success('已登记，已通知提交人领取')
    registerVisible.value = false
    load()
  } finally {
    submitting.value = false
  }
}

/**
 * 打开撤销弹窗
 * @param row 报销单
 */
function openCancel(row: any) {
  current.value = row
  cancelReason.value = ''
  cancelVisible.value = true
}

/**
 * 提交撤销
 */
async function onCancel() {
  if (!current.value) return
  submitting.value = true
  try {
    await cancelRegistration(current.value.id, { reason: cancelReason.value || undefined })
    ElMessage.success('已撤销登记')
    cancelVisible.value = false
    load()
  } finally {
    submitting.value = false
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
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}
</style>
