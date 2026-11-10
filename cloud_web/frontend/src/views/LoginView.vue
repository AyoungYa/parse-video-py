<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { api } from "@/api/client";
import { useUserStore } from "@/stores/user";
import { useSiteStore } from "@/stores/site";
import BrandLogo from "@/components/BrandLogo.vue";

const router = useRouter();
const user = useUserStore();
const site = useSiteStore();

const phone = ref("");
const code = ref("");
const loading = ref(false);
const sending = ref(false);
const errorMsg = ref("");
const countdown = ref(0);
/** 本地开发（console 短信通道）时后端回显的验证码，方便联调 */
const devCode = ref("");

/** 图形验证码：服务端签发的挑战，答案不回传（本地开发回显在 devCaptcha） */
const captchaId = ref("");
const captchaCode = ref("");
const captchaImg = ref("");
const captchaLoading = ref(false);
const devCaptcha = ref("");

let timer: number | undefined;

const RESEND_SECONDS = 60;
const SENT_AT_KEY = "sms_sent_at";

const phoneValid = computed(() => /^1[3-9]\d{9}$/.test(phone.value));
const canSend = computed(
  () => phoneValid.value && captchaCode.value.length === 4 && countdown.value === 0 && !sending.value
);
const canSubmit = computed(() => phoneValid.value && /^\d{6}$/.test(code.value) && !loading.value);

const SELLING = [
  { k: "擦得干净", v: "mask 含半透明柔边，不只擦最白的那层" },
  { k: "验得了货", v: "残留 / 纹理 / 闪烁三项指标，附报告" },
  { k: "不达标不计费", v: "任何一项没过线，本次免费" },
];

/* 备案号（工信部要求页底展示）。备案通过后替换成真实号码并把 HAS_ICP 改 true。 */
const ICP_NUMBER = "京ICP备00000000号";
const HAS_ICP = false;

function startCountdown(seconds: number) {
  countdown.value = Math.max(1, Math.ceil(seconds));
  localStorage.setItem(SENT_AT_KEY, String(Date.now()));
  window.clearInterval(timer);
  timer = window.setInterval(() => {
    countdown.value -= 1;
    if (countdown.value <= 0) {
      window.clearInterval(timer);
      localStorage.removeItem(SENT_AT_KEY);
      countdown.value = 0;
    }
  }, 1000);
}

// 刷新页面后继续保持倒计时，避免用户反复点发送
const lastSent = Number(localStorage.getItem(SENT_AT_KEY) || 0);
if (lastSent) {
  const left = RESEND_SECONDS - Math.floor((Date.now() - lastSent) / 1000);
  if (left > 0) startCountdown(left);
  else localStorage.removeItem(SENT_AT_KEY);
}

onBeforeUnmount(() => window.clearInterval(timer));

/** 取一张新的图形验证码。每次发送（无论成败）后都要换新图：旧挑战已核销/作废。 */
async function refreshCaptcha() {
  captchaLoading.value = true;
  try {
    const { data } = await api.captcha();
    captchaId.value = data.captcha_id;
    captchaImg.value = data.image;
    devCaptcha.value = data.dev_code || "";
    captchaCode.value = "";
  } catch {
    captchaImg.value = "";
  } finally {
    captchaLoading.value = false;
  }
}

onMounted(refreshCaptcha);

async function sendCode() {
  if (!canSend.value) return;
  errorMsg.value = "";
  if (!phoneValid.value) {
    errorMsg.value = "请输入正确的 11 位手机号";
    return;
  }
  sending.value = true;
  try {
    const { data } = await api.smsSend(phone.value, captchaId.value, captchaCode.value);
    startCountdown(data.retry_after || RESEND_SECONDS);
    devCode.value = data.dev_code || "";
    ElMessage.success("验证码已发送");
  } catch (e: any) {
    const retry = e?.response?.data?.retry_after;
    if (retry) startCountdown(retry);
    errorMsg.value = e?.response?.data?.message || "发送失败，请稍后重试";
  } finally {
    // 无论如何都换新图：图形码是一次性的（成功已核销，失败也可能已作废）
    await refreshCaptcha();
    sending.value = false;
  }
}

async function submit() {
  if (loading.value) return;
  errorMsg.value = "";
  if (!phoneValid.value) {
    errorMsg.value = "请输入正确的 11 位手机号";
    return;
  }
  if (!/^\d{6}$/.test(code.value)) {
    errorMsg.value = "请输入 6 位验证码";
    return;
  }

  loading.value = true;
  try {
    const { data } = await api.smsLogin(phone.value, code.value);
    user.setToken(data.token);
    await user.refresh();
    if (data.is_new) ElMessage.success("账号已创建，试用额度已到账");
    router.push({ name: "upload" });
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.message || "登录失败，请重试";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="auth">
    <aside class="brand-side">
      <div class="brand-row">
        <BrandLogo :size="30" inverse />
        <span class="brand-name">无痕匠</span>
      </div>

      <div class="brand-body">
        <h1 class="slogan">擦得干净，<br />验得了货。</h1>
        <ul class="selling">
          <li v-for="s in SELLING" :key="s.k">
            <span class="k">{{ s.k }}</span>
            <span class="v">{{ s.v }}</span>
          </li>
        </ul>
      </div>

      <p class="legal">
        仅限处理您拥有版权或已获授权的素材。本站不提供任何平台视频解析或下载功能，素材在任务结束
        {{ site.retentionText }}后自动删除。
      </p>
    </aside>

    <main class="form-side">
      <div class="form-wrap">
        <div class="mobile-brand">
          <BrandLogo :size="28" inverse />
          无痕匠
        </div>

        <h2 class="h1">手机号登录</h2>
        <p class="micro muted sub">未注册的手机号将自动创建账号，新用户注册即赠试用额度。</p>

        <form class="form" @submit.prevent="submit">
          <label class="field">
            <span class="field-label">手机号</span>
            <div class="input-group">
              <span class="prefix num">+86</span>
              <input
                v-model="phone"
                class="input num"
                type="tel"
                inputmode="numeric"
                maxlength="11"
                placeholder="11 位手机号"
                autocomplete="tel"
                autofocus
              />
            </div>
          </label>

          <label class="field">
            <span class="field-label">图形验证码</span>
            <div class="code-row">
              <input
                v-model="captchaCode"
                class="input num"
                type="text"
                maxlength="4"
                placeholder="4 位字符"
                autocomplete="off"
              />
              <button
                class="captcha-btn"
                type="button"
                :disabled="captchaLoading"
                title="看不清？点击换一张"
                @click="refreshCaptcha"
              >
                <img v-if="captchaImg" :src="captchaImg" alt="图形验证码，点击刷新" />
                <span v-else class="micro muted">加载中…</span>
              </button>
            </div>
          </label>

          <label class="field">
            <span class="field-label">验证码</span>
            <div class="code-row">
              <input
                v-model="code"
                class="input num"
                type="text"
                inputmode="numeric"
                maxlength="6"
                placeholder="6 位验证码"
                autocomplete="one-time-code"
              />
              <button
                class="btn btn-secondary send-btn num"
                type="button"
                :disabled="!canSend"
                @click="sendCode"
              >
                {{ countdown > 0 ? `${countdown}s 后重发` : sending ? "发送中…" : "获取验证码" }}
              </button>
            </div>
          </label>

          <p v-if="devCaptcha" class="dev-hint micro">
            本地开发图形码：<b class="num">{{ devCaptcha }}</b>
            <button class="link-btn" type="button" @click="captchaCode = devCaptcha">填入</button>
          </p>

          <p v-if="devCode" class="dev-hint micro">
            本地开发验证码：<b class="num">{{ devCode }}</b>
            <button class="link-btn" type="button" @click="code = devCode">填入</button>
          </p>

          <p v-if="errorMsg" class="err">{{ errorMsg }}</p>

          <button class="btn btn-primary btn-lg btn-block" type="submit" :disabled="!canSubmit">
            {{ loading ? "登录中…" : "登录" }}
          </button>
        </form>

        <p class="hint micro muted">
          登录即表示同意《用户协议》与《隐私政策》。我们只处理您上传的素材，任务结束
          {{ site.retentionText }}后自动删除。
        </p>

        <p class="legal-mobile legal">
          仅限处理您拥有版权或已获授权的素材。本站不提供任何平台视频解析或下载功能。
        </p>

        <!-- 备案号（工信部要求页底展示；登录页是分栏布局，不挂通栏页脚） -->
        <p class="beian micro">
          <a
            v-if="HAS_ICP"
            href="https://beian.miit.gov.cn/"
            target="_blank"
            rel="noopener noreferrer"
            >{{ ICP_NUMBER }}</a
          >
          <span v-else>ICP 备案办理中</span>
        </p>
      </div>
    </main>
  </div>
</template>

<style scoped>
.auth {
  min-height: 100vh;
  display: grid;
  grid-template-columns: minmax(340px, 42%) 1fr;
}

/* ── 左侧品牌区 ─────────────────────────── */
.brand-side {
  background: #0b3f33;
  color: #eef4f1;
  padding: 40px 44px 36px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

.brand-row {
  display: flex;
  align-items: center;
  gap: 9px;
}

.brand-name {
  font-size: var(--fs-md);
  font-weight: 500;
  letter-spacing: 0.06em;
  color: #f2f7f4;
}

.slogan {
  font-size: var(--fs-hero);
  line-height: 1.28;
  font-weight: 500;
  letter-spacing: -0.01em;
  color: #ffffff;
  margin-bottom: 32px;
}

.selling {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.selling li {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-left: 14px;
  border-left: 1.5px solid rgba(127, 208, 180, 0.4);
}

.selling .k {
  font-size: var(--fs-small);
  font-weight: 500;
  color: #d9ebe4;
}

.selling .v {
  font-size: var(--fs-micro);
  color: rgba(217, 235, 228, 0.62);
  line-height: 1.55;
}

.legal {
  font-size: var(--fs-caption);
  line-height: 1.7;
  color: rgba(217, 235, 228, 0.42);
}

/* ── 右侧表单 ───────────────────────────── */
.form-side {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 24px;
  background: var(--paper);
}

.form-wrap {
  width: 100%;
  max-width: 344px;
}

.mobile-brand {
  display: none;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-md);
  font-weight: 500;
  color: var(--ink-900);
  letter-spacing: 0.04em;
  margin-bottom: 28px;
}

.mobile-brand .mark {
  color: var(--brand);
}

.sub {
  margin: 8px 0 26px;
  line-height: 1.65;
}

.form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 手机号：+86 前缀与输入框拼成一个控件 */
.input-group {
  display: flex;
  align-items: center;
  border: 1px solid var(--line-strong);
  border-radius: var(--r-ctl);
  background: var(--surface);
  transition: border-color 0.16s, box-shadow 0.16s;
}

.input-group:focus-within {
  border-color: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-ring);
}

.input-group .prefix {
  padding: 0 10px 0 12px;
  font-size: var(--fs-body);
  color: var(--ink-400);
  border-right: 1px solid var(--line-soft);
  line-height: 1.2;
}

.input-group .input {
  border: 0;
  background: transparent;
  box-shadow: none;
  padding-left: 10px;
}

.input-group .input:focus {
  box-shadow: none;
  border: 0;
}

.code-row {
  display: flex;
  gap: 8px;
}

.code-row .input {
  flex: 1;
  min-width: 0;
  letter-spacing: 0.14em;
}

.send-btn {
  height: var(--ctl-h-input);
  min-width: 150px;
  font-size: var(--fs-small);
}

/* 图形验证码图片：占输入框同高，点击整块换新图 */
.captcha-btn {
  height: var(--ctl-h-input);
  width: 176px;
  flex: none;
  padding: 0;
  border: 1px solid var(--line-strong);
  border-radius: var(--r-ctl);
  background: var(--surface);
  cursor: pointer;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}

.captcha-btn img {
  width: 168px;
  height: 56px;
  display: block;
}

.captcha-btn:active {
  border-color: var(--brand);
}

.dev-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: -6px;
  color: var(--ink-400);
}

.dev-hint .link-btn {
  margin-left: 2px;
}

.dev-hint b {
  color: var(--brand);
  letter-spacing: 0.1em;
}

.err {
  font-size: var(--fs-micro);
  color: var(--bad);
  margin-top: -4px;
}

.hint {
  margin-top: 18px;
  line-height: 1.7;
}

.legal-mobile {
  display: none;
}

/* 备案号：页底展示（工信部要求），样式与 legal 说明保持同一层级 */
.beian {
  margin-top: 14px;
  color: var(--ink-300);
  line-height: 1.6;
}

.beian a {
  color: var(--ink-400);
  text-decoration: none;
}

.beian a:hover {
  color: var(--brand);
  text-decoration: underline;
}

@media (max-width: 880px) {
  .auth {
    grid-template-columns: 1fr;
  }

  .brand-side {
    display: none;
  }

  .mobile-brand {
    display: flex;
  }

  .legal-mobile {
    display: block;
    margin-top: 36px;
    color: var(--ink-300);
  }

  .form-side {
    padding: 32px 20px 48px;
    align-items: flex-start;
  }
}
</style>
