<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">
        <div class="brand-logo">AI</div>
        <h1>财务报销审核系统</h1>
        <p class="muted">AI Agent · 规则校验 · 风险预警</p>
      </div>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="用户名 / 邮箱" prop="username">
          <el-input v-model="form.username" size="large" placeholder="请输入用户名或邮箱" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            size="large"
            show-password
            placeholder="请输入密码"
            autocomplete="current-password"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button type="primary" size="large" class="submit" :loading="loading" @click="onSubmit">
          登录
        </el-button>
      </el-form>

      <p class="tip muted">首次登录会被要求修改初始密码</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const route = useRoute()
const store = useUserStore()

const formRef = ref<FormInstance>()
const loading = ref(false)

// 登录表单
const form = reactive({
  username: '',
  password: '',
})

// 校验规则
const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名或邮箱', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

/**
 * 提交登录
 */
async function onSubmit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await store.login(form.username.trim(), form.password)
    ElMessage.success('登录成功')
    const redirect = (route.query.redirect as string) || '/dashboard'
    router.replace(redirect)
  } catch {
    // 错误提示已由 axios 拦截器统一弹出
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1f2d3d 0%, #2b4c7e 100%);
}

.login-card {
  width: 380px;
  background: #fff;
  border-radius: 12px;
  padding: 32px 28px 20px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18);
}

.brand {
  text-align: center;
  margin-bottom: 20px;
}

.brand-logo {
  width: 46px;
  height: 46px;
  margin: 0 auto 10px;
  border-radius: 12px;
  background: #409eff;
  color: #fff;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}

.brand h1 {
  font-size: 18px;
  margin: 0 0 4px;
}

.submit {
  width: 100%;
  margin-top: 4px;
}

.tip {
  text-align: center;
  margin-top: 14px;
}
</style>
