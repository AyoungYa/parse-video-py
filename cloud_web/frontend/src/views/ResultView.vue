<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import CompareSlider from "@/components/CompareSlider.vue";
import MetricReport from "@/components/MetricReport.vue";
import TaskTimeline from "@/components/TaskTimeline.vue";
import TaskStageContent from "@/components/TaskStageContent.vue";
import { api, formatDuration, formatSize, type TaskDetail } from "@/api/client";
import { useSiteStore } from "@/stores/site";

const route = useRoute();
const router = useRouter();
const site = useSiteStore();
const taskId = String(route.params.id);

const task = ref<TaskDetail | null>(null);
const loadError = ref("");
const cancelling = ref(false);
const retrying = ref(false);
const now = ref(Date.now());

let pollTimer: number | undefined;
let tickTimer: number | undefined;

const TERMINAL = ["succeeded", "failed", "cancelled", "expired"];

const isTerminal = computed(() => TERMINAL.includes(task.value?.status ?? ""));
const isProcessing = computed(
  () => !isTerminal.value && ["pending", "leased", "extracting_frames", "locating_watermark", "awaiting_confirmation", "needs_manual", "removing_watermark", "running", "verifying"].includes(task.value?.status ?? "")
);

const headline = computed(() => {
  const s = task.value?.status;
  if (s === "succeeded") return "已完成";
  if (s === "failed") return "处理失败";
  if (s === "cancelled") return "已取消";
  if (s === "expired") return "已过期";
  if (s === "needs_manual" || s === "awaiting_confirmation") return "需要你确认水印位置";
  if (s === "extracting_frames") return "获取关键帧";
  if (s === "locating_watermark") return "定位水印";
  if (s === "removing_watermark") return "去水印中";
  if (s === "verifying") return "验收中";
  if ((s === "pending" || s === "leased") && task.value?.params?.bbox_confirmed) return "等待去水印";
  if (s === "running" || s === "leased") return "处理中";
  return "排队中";
});

const elapsedText = computed(() => {
  const t = task.value;
  if (!t?.created_at) return "";
  const start = new Date(t.created_at).getTime();
  const end = t.finished_at ? new Date(t.finished_at).getTime() : now.value;
  const sec = Math.max(0, Math.round((end - start) / 1000));
  if (sec < 60) return `${sec} 秒`;
  return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
});

const remainingSeconds = computed<number | null>(() => {
  const t = task.value;
  if (!t || !["running", "removing_watermark"].includes(t.status) || !t.progress) return null;
  const startStr = t.started_at || t.created_at;
  if (!startStr) return null;
  const elapsed = (now.value - new Date(startStr).getTime()) / 1000;
  if (elapsed <= 2) return null;
  const total = elapsed / t.progress;
  return Math.max(0, Math.round(total - elapsed));
});

// 排队超过 90 秒还没被领走，基本就是算力机掉线了。前端推断，不打扰用户也足够诚实。
const pendingSince = ref<number | null>(null);
watch(
  () => task.value?.status,
  (s) => {
    if (s === "pending" && pendingSince.value === null) pendingSince.value = Date.now();
    if (s !== "pending") pendingSince.value = null;
  }
);
const workerOffline = computed(
  () => pendingSince.value !== null && now.value - pendingSince.value > 90_000
);

const aspect = computed(() => {
  const t = task.value;
  if (!t?.width || !t?.height) return 16 / 9;
  return t.width / t.height;
});

const canCompare = computed(
  () => Boolean(task.value?.preview_url && task.value?.download_url)
);

const beforeUrl = computed(() => task.value?.preview_url ?? "");
const afterUrl = computed(() => task.value?.download_url ?? "");

const riskCount = computed(() => {
  const m = task.value?.metrics ?? {};
  const rules: [string, (v: number) => boolean][] = [
    ["residual_max", (v) => v < 3],
    ["gradient_p50_ratio", (v) => v > 0.85],
    ["flicker_p90", (v) => v < 1.5],
  ];
  return rules.filter(([k, ok]) => typeof m[k] === "number" && !ok(m[k] as number)).length;
});

async function poll() {
  try {
    const { data } = await api.taskDetail(taskId);
    task.value = data;
    loadError.value = "";
    if (TERMINAL.includes(data.status)) stopPolling();
  } catch (e: any) {
    if (e?.response?.status === 404) {
      loadError.value = "找不到这条任务，可能已被清理。";
      stopPolling();
    }
  }
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = undefined;
}

async function cancel() {
  if (cancelling.value) return;
  cancelling.value = true;
  try {
    await api.cancelTask(taskId);
    await poll();
  } finally {
    cancelling.value = false;
  }
}

function again() {
  router.push({ name: "upload" });
}

async function retry() {
  if (retrying.value) return;
  retrying.value = true;
  try {
    await api.retryTask(taskId);
    await poll();
    if (!pollTimer) pollTimer = window.setInterval(poll, 2000);
  } finally {
    retrying.value = false;
  }
}

onMounted(() => {
  void poll();
  pollTimer = window.setInterval(poll, 2000);
  tickTimer = window.setInterval(() => {
    now.value = Date.now();
  }, 5000);
});

onBeforeUnmount(() => {
  stopPolling();
  if (tickTimer) window.clearInterval(tickTimer);
});
</script>

<template>
  <div class="page">
    <div v-if="loadError" class="card">
      <p class="h2">{{ loadError }}</p>
      <button class="btn btn-primary" style="margin-top: 16px" type="button" @click="again">
        重新上传
      </button>
    </div>

    <template v-else-if="task">
      <!-- 页头 -->
      <div class="head">
        <div class="grow">
          <div class="row" style="gap: 10px; flex-wrap: wrap">
            <h1 class="h1">{{ headline }}</h1>
            <span
              v-if="task.status === 'succeeded'"
              class="tag"
              :class="task.all_green ? 'tag-ok' : 'tag-warn'"
            >
              {{ task.all_green ? "三项全部通过" : `${riskCount} 项需留意` }}
            </span>
            <span v-else-if="isProcessing && remainingSeconds !== null" class="tag tag-brand num">
              约剩 {{ remainingSeconds }} 秒
            </span>
          </div>
          <div class="micro muted num meta-line">
            {{ formatDuration(task.duration_seconds) }}
            <template v-if="task.width">　·　{{ task.height > task.width ? "竖屏" : "横屏" }} {{ task.width }}×{{ task.height }}</template>
            <template v-if="task.size_bytes">　·　{{ formatSize(task.size_bytes) }}</template>
            <template v-if="elapsedText">　·　{{ isProcessing ? "已用" : "用时" }} {{ elapsedText }}</template>
          </div>
        </div>
        <button class="link-btn" type="button" @click="again">再传一条</button>
      </div>

      <!-- 处理中 -->
      <div v-if="isProcessing" class="card">
        <TaskTimeline
          :status="task.status"
          :progress="task.progress"
          :confirmed="Boolean(task.params?.bbox_confirmed)"
        />
        <TaskStageContent :task="task" :worker-offline="workerOffline" @updated="poll" />
        <hr class="divider" />
        <div class="row-between">
          <span class="micro muted">
            页面可以关闭，回来还能看到进度。
          </span>
          <button
            v-if="['pending', 'leased', 'awaiting_confirmation', 'needs_manual'].includes(task.status)"
            class="link-btn"
            type="button"
            :disabled="cancelling"
            @click="cancel"
          >
            {{ cancelling ? "取消中…" : "取消并退还积分" }}
          </button>
        </div>
      </div>

      <!-- 成功 -->
      <template v-else-if="task.status === 'succeeded'">
        <div class="card success-stage">
          <TaskTimeline :status="task.status" :progress="1" :confirmed="true" />
          <div class="success-heading"><span class="eyebrow">质量验收</span><h2 class="h2">原片与成片，对照查看</h2><p class="small secondary">拖动分割线查看同一画面中的修复效果，也可以切换为只看成片。</p></div>
        <div v-if="canCompare" class="compare-block">
          <CompareSlider :before="beforeUrl" :after="afterUrl" :aspect="aspect" />
        </div>
        <div v-else class="notice" style="margin-bottom: 16px">
          预览链接已过期，可以刷新页面重新获取，或直接下载成片。
        </div>
        </div>

        <MetricReport
          :metrics="task.metrics || {}"
          :all-green="task.all_green"
          style="margin-top: 20px"
        />

        <div class="actions">
          <a
            v-if="task.download_url"
            class="btn btn-primary btn-lg"
            :href="task.download_url"
            target="_blank"
            rel="noopener"
          >
            <svg viewBox="0 0 14 14" aria-hidden="true" class="dl-icon">
              <path d="M7 2.6v6.6M7 9.2L4.4 6.6M7 9.2l2.6-2.6M2.8 11.4h8.4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            下载成片
          </a>
          <a
            v-if="task.report_url"
            class="btn btn-secondary btn-lg"
            :href="task.report_url"
            target="_blank"
            rel="noopener"
          >
            报告 JSON
          </a>
          <span class="micro muted action-hint">链接 10 分钟内有效 · 成片 {{ site.retentionText }}后自动删除</span>
        </div>
      </template>

      <!-- 失败 / 取消 / 过期 -->
      <div v-else class="card">
        <p class="small" style="color: var(--ink-900)">
          {{ task.status === "failed" ? "系统原因处理失败。" : task.error_message || (task.status === "expired" ? `素材已超过 ${site.retentionText} 保留期，已自动清理。` : "任务已结束。") }}
        </p>
        <p v-if="task.status === 'failed' && task.can_retry" class="small muted" style="margin-top: 8px">
          原视频已保存，可直接重新处理，无需再次上传。
        </p>
        <p class="micro muted" style="margin-top: 8px">
          <template v-if="task.status === 'failed'">
            {{ task.points_cost ? `本次消耗的 ${task.points_cost} 积分已自动退还。` : "本次没有产生扣费。" }}
          </template>
          <template v-else-if="task.status === 'cancelled'">
            {{ task.points_cost ? "积分已退还。" : "" }}
          </template>
        </p>
        <div class="row" style="margin-top: 18px">
          <button v-if="task.status === 'failed' && task.can_retry" class="btn btn-primary" type="button" :disabled="retrying" @click="retry">
            {{ retrying ? "正在重新排队…" : "重新处理" }}
          </button>
          <button v-else class="btn btn-primary" type="button" @click="again">上传新视频</button>
        </div>
      </div>
    </template>

    <div v-else class="card skeleton">
      <div class="sk-line" style="width: 40%; height: 20px" />
      <div class="sk-line" style="width: 70%; margin-top: 12px" />
      <div class="sk-block" />
    </div>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
  margin-bottom: var(--sp-5);
}

.meta-line {
  margin-top: 6px;
}

.compare-block {
  border-radius: var(--r-card);
}
.success-heading { padding: 24px 0 18px; border-top: 1px solid var(--line-soft); }
.success-heading .h2 { margin: 7px 0; }

.actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 18px;
  flex-wrap: wrap;
}

.dl-icon {
  width: 14px;
  height: 14px;
}

.action-hint {
  margin-left: auto;
}

/* 骨架屏：首屏 3 次轮询之内就会消失，比转圈的观感稳 */
.skeleton {
  animation: pulse 1.4s ease-in-out infinite;
}

.sk-line {
  height: 12px;
  border-radius: 4px;
  background: var(--line-soft);
}

.sk-block {
  height: 220px;
  border-radius: 10px;
  background: var(--line-soft);
  margin-top: 20px;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.6;
  }
}

@media (max-width: 640px) {
  .head {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }

  .head > .link-btn {
    align-self: flex-start;
  }

  .actions {
    flex-direction: column;
    align-items: stretch;
  }

  .action-hint {
    margin-left: 0;
  }
}
</style>
