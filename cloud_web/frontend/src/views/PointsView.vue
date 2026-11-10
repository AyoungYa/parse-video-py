<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "@/api/client";
import { useUserStore } from "@/stores/user";

interface Entry {
  delta: number;
  balance_after: number;
  reason: string;
  note: string;
  created_at: string;
}

interface CheckIn {
  signed_today: boolean;
  streak: number;
  month_earned: number;
  month_cap: number;
}

interface Subscription {
  plan: string;
  expires_at: string;
  minutes_used: number;
  minutes_limit: number;
}

interface PointsData {
  balance: number;
  check_in: CheckIn;
  subscription: Subscription | null;
  entries: Entry[];
}

const user = useUserStore();
const data = ref<PointsData | null>(null);
const signing = ref(false);
const loadFailed = ref(false);

const REASON: Record<string, string> = {
  initial: "新人赠送",
  sign_in: "每日签到",
  streak: "连签奖励",
  consume: "任务消耗",
  refund: "失败退还",
  subscribe: "订阅赠送",
};

const checkIn = computed<CheckIn>(
  () => data.value?.check_in ?? { signed_today: false, streak: 0, month_earned: 0, month_cap: 10 }
);

/** 连签进度在一周内的位置：第 7 天有额外奖励 */
const dots = computed(() => {
  const s = checkIn.value.streak;
  const inCycle = s === 0 ? 0 : s % 7 === 0 ? 7 : s % 7;
  return Array.from({ length: 7 }, (_, i) => i < inCycle);
});

const balanceHint = computed(() => {
  const b = data.value?.balance ?? 0;
  if (data.value?.subscription) return "订阅期内处理不扣积分";
  if (b === 0) return "积分用完了，签到或订阅后继续";
  // 刻意不给「1 分 = 1 条 30 秒 / 5 分钟 = 3 分」这类明确换算，方便后续调价
  return `当前积分约可处理 ${b} 条短视频，更长的视频会按时长多扣积分`;
});

const subHint = computed(() => {
  const sub = data.value?.subscription;
  if (!sub?.expires_at) return "";
  const days = Math.max(
    0,
    Math.ceil((new Date(sub.expires_at).getTime() - Date.now()) / 86_400_000)
  );
  return days > 0 ? `剩余 ${days} 天` : "即将到期";
});

const usageRatio = computed(() => {
  const sub = data.value?.subscription;
  if (!sub?.minutes_limit) return 0;
  return Math.min(100, (sub.minutes_used / sub.minutes_limit) * 100);
});

const capRatio = computed(() => {
  const c = checkIn.value;
  if (!c.month_cap) return 0;
  return Math.min(100, (c.month_earned / c.month_cap) * 100);
});

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function load() {
  try {
    const { data: res } = await api.points();
    data.value = res as PointsData;
    user.points = data.value.balance;
    loadFailed.value = false;
  } catch {
    loadFailed.value = true;
  }
}

async function signIn() {
  if (signing.value || checkIn.value.signed_today) return;
  signing.value = true;
  try {
    const { data: r } = await api.signIn();
    if (r.already) {
      ElMessage.info("今天已经签过了");
    } else if (r.capped) {
      ElMessage.warning("本月签到积分已达上限，下月恢复");
    } else {
      ElMessage.success(`签到 +${r.points} 分，已连续 ${r.streak} 天`);
    }
    await load();
    await user.refresh().catch(() => undefined);
  } finally {
    signing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="page page-mid">
    <div class="head">
      <div>
        <h1 class="h1">账户</h1>
        <p class="micro muted" style="margin-top: 4px">积分、签到与处理记录</p>
      </div>
      <router-link :to="{ name: 'upload' }" class="btn btn-secondary back-btn">← 回到工作台</router-link>
    </div>

    <div v-if="loadFailed" class="notice notice-bad">加载失败，请刷新页面重试。</div>

    <!-- 余额 -->
    <div class="card">
      <div class="row-between">
        <span class="eyebrow">积分余额</span>
        <span v-if="data?.subscription" class="tag tag-brand">包月 · {{ subHint }}</span>
      </div>
      <div class="balance num">{{ data?.balance ?? "—" }}</div>
      <div class="micro muted">{{ balanceHint }}</div>

      <hr class="divider" />

      <div class="row-between" style="gap: 16px; flex-wrap: wrap">
        <div class="grow">
          <div class="micro" style="color: var(--ink-900)">
            连续签到 <span class="num">{{ checkIn.streak }}</span> 天
          </div>
          <div class="micro muted" style="margin-top: 2px">
            签到 +1/天，连签 7 天额外 +5，每月最多 {{ checkIn.month_cap }} 分
          </div>
        </div>
        <button
          class="btn"
          :class="checkIn.signed_today ? 'btn-secondary' : 'btn-primary'"
          type="button"
          :disabled="signing || checkIn.signed_today"
          @click="signIn"
        >
          {{ checkIn.signed_today ? "今天已签到" : signing ? "签到中…" : "签到" }}
        </button>
      </div>

      <div class="days" aria-hidden="true">
        <span v-for="(on, i) in dots" :key="i" class="day" :class="{ on, bonus: i === 6 }">
          {{ i === 6 ? "+5" : i + 1 }}
        </span>
      </div>

      <div class="cap">
        <div class="bar"><i :style="{ width: `${Math.max(2, capRatio)}%` }" /></div>
        <span class="micro muted num">本月签到已得 {{ checkIn.month_earned }} / {{ checkIn.month_cap }} 分</span>
      </div>
    </div>

    <!-- 订阅 -->
    <div v-if="data?.subscription" class="card">
      <div class="row-between" style="margin-bottom: 12px">
        <span class="h2">订阅用量</span>
        <span class="micro muted num">
          {{ Math.round(data.subscription.minutes_used) }} / {{ data.subscription.minutes_limit }} 分钟
        </span>
      </div>
      <div class="bar"><i :style="{ width: `${Math.max(2, usageRatio)}%` }" /></div>
      <p class="micro muted" style="margin-top: 10px">
        单条上限 30 分钟，支持 4K 与优先队列。超出月度上限后可加购分钟数。
      </p>
    </div>

    <!-- 流水 -->
    <section style="margin-top: var(--sp-6)">
      <span class="eyebrow">积分流水</span>

      <div v-if="!data?.entries?.length" class="empty micro muted">
        还没有记录。上传第一条视频后，这里会显示每次消耗与退还。
      </div>

      <ul v-else class="ledger">
        <li v-for="(e, i) in data.entries" :key="i" class="entry">
          <div class="grow">
            <div class="reason">{{ REASON[e.reason] ?? e.reason }}</div>
            <div class="micro muted">
              {{ formatDate(e.created_at) }}<template v-if="e.note">　·　{{ e.note }}</template>
            </div>
          </div>
          <span class="delta num" :class="{ plus: e.delta > 0 }">
            {{ e.delta > 0 ? "+" : "" }}{{ e.delta }}
          </span>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.page-mid {
  max-width: 640px;
}

.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: var(--sp-5);
}
.back-btn { white-space: nowrap; }
@media (max-width: 480px) {
  .head { align-items: flex-start; }
  .back-btn { padding-left: 10px; padding-right: 10px; }
}

.balance {
  font-size: var(--fs-huge);
  line-height: 1.1;
  font-weight: 500;
  letter-spacing: -0.02em;
  margin: 6px 0 4px;
}

.days {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}

.day {
  flex: 1;
  height: 40px;
  border-radius: 6px;
  background: var(--surface-sunken);
  color: var(--ink-300);
  font-size: var(--fs-caption);
  display: flex;
  align-items: center;
  justify-content: center;
}

.day.on {
  background: var(--brand-wash);
  color: var(--brand);
}

.day.bonus {
  border: 1px dashed var(--line-strong);
}

.day.bonus.on {
  border-style: solid;
  border-color: var(--brand);
}

.cap {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.empty {
  padding: 20px 0;
}

.ledger {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
}

.entry {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 0;
  border-bottom: 1px solid var(--line-soft);
}

.entry:last-child {
  border-bottom: 0;
}

.reason {
  font-size: var(--fs-small);
  color: var(--ink-900);
}

.delta {
  font-size: var(--fs-body);
  color: var(--ink-600);
  flex-shrink: 0;
}

.delta.plus {
  color: var(--ok);
}
</style>
