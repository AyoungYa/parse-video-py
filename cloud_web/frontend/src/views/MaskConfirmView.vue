<script setup lang="ts">
/**
 * 人工确认水印位置（M3.1）。
 *
 * 触发：worker 自动定位信心不足 → 任务转 needs_manual → 结果页引导到这里。
 * 出口：提交 bbox → 任务回 pending，worker 跳过自动定位直接精修；不重复扣分。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import BoxSelector from "@/components/BoxSelector.vue";
import TaskStatusPill from "@/components/TaskStatusPill.vue";
import { api, type BBox, type TaskDetail } from "@/api/client";

const route = useRoute();
const router = useRouter();
const taskId = String(route.params.id);

const task = ref<TaskDetail | null>(null);
const bbox = ref<BBox | null>(null);
const loading = ref(true);
const submitting = ref(false);
const cancelling = ref(false);
const loadError = ref("");

const frameW = computed(() => task.value?.frame_width || task.value?.width || 1920);
const frameH = computed(() => task.value?.frame_height || task.value?.height || 1080);
const candidates = computed(() => task.value?.locate_candidates ?? []);

const boxValid = computed(() => {
  const b = bbox.value;
  if (!b) return false;
  const [x, y, w, h] = b;
  return (
    x >= 0 &&
    y >= 0 &&
    w >= 8 &&
    h >= 8 &&
    x + w <= frameW.value &&
    y + h <= frameH.value
  );
});

async function load() {
  loading.value = true;
  try {
    const { data } = await api.taskDetail(taskId);
    if (data.status !== "needs_manual") {
      // 状态已经变了（别的标签页提交过 / worker 已恢复），别停在过时页面
      await router.replace({ name: "result", params: { id: taskId } });
      return;
    }
    task.value = data;
    loadError.value = "";
    // 有候选框就先套第一个：多数情况只需微调，不必从零画
    const first = data.locate_candidates?.[0];
    if (first && !bbox.value) bbox.value = [first.x, first.y, first.w, first.h];
  } catch (e: any) {
    loadError.value =
      e?.response?.status === 404 ? "找不到这条任务，可能已被清理。" : "任务信息加载失败，请刷新重试。";
  } finally {
    loading.value = false;
  }
}

async function submit() {
  if (!boxValid.value || submitting.value) return;
  submitting.value = true;
  try {
    await api.submitBbox(taskId, bbox.value!);
    ElMessage.success("位置已提交，重新排队处理");
    await router.push({ name: "result", params: { id: taskId } });
  } catch (e: any) {
    // http 拦截器已经弹了后端 message；409 说明状态变了，回结果页看最新状态
    if (e?.response?.status === 409) {
      await router.replace({ name: "result", params: { id: taskId } });
    }
  } finally {
    submitting.value = false;
  }
}

async function cancel() {
  if (cancelling.value) return;
  cancelling.value = true;
  try {
    await api.cancelTask(taskId);
    await router.push({ name: "result", params: { id: taskId } });
  } finally {
    cancelling.value = false;
  }
}

function refreshPreview() {
  bbox.value = null;
  void load();
}

onMounted(load);
</script>

<template>
  <div class="page">
    <div v-if="loadError" class="card">
      <p class="h2">{{ loadError }}</p>
      <button class="btn btn-primary" style="margin-top: 16px" type="button" @click="load">重试</button>
    </div>

    <div v-else-if="loading" class="card skeleton">
      <div class="sk-line" style="width: 45%; height: 20px" />
      <div class="sk-line" style="width: 75%; margin-top: 12px" />
      <div class="sk-block" />
    </div>

    <template v-else-if="task">
      <div class="head">
        <div class="grow">
          <div class="row" style="gap: 10px; flex-wrap: wrap">
            <h1 class="h1">麻烦你框一下水印</h1>
            <TaskStatusPill status="needs_manual" />
          </div>
          <p class="small secondary" style="margin-top: 8px">
            这条视频的水印位置自动识别没把握。你框出大致范围，我们接着处理，<strong>不会重复扣积分</strong>。
          </p>
        </div>
        <button class="link-btn" type="button" @click="refreshPreview">刷新预览</button>
      </div>

      <div v-if="task.locate_reason" class="notice notice-warn" style="margin-bottom: 16px">
        <span class="micro">识别情况：{{ task.locate_reason }}</span>
      </div>

      <div class="card">
        <div class="card-head">
          <span class="eyebrow">框选水印区域</span>
          <span class="micro muted">坐标系：原片 {{ frameW }} × {{ frameH }} px</span>
        </div>

        <BoxSelector
          v-model="bbox"
          :video-url="task.preview_url || ''"
          :width="frameW"
          :height="frameH"
          :candidates="candidates"
          :duration-seconds="task.duration_seconds || 10"
        />

        <div class="tips micro muted">
          <p>· 框要<strong>完整盖住水印</strong>，包含半透明的柔边；略大一点比切边好。</p>
          <p>· 只框水印，别把人脸、字幕一起框进去，框太大处理会变慢也容易留痕。</p>
          <p>· 片头常是黑场看不清，拖下方「画面位置」换一帧再框。</p>
        </div>
      </div>

      <div class="actions">
        <button
          class="btn btn-primary btn-lg"
          type="button"
          :disabled="!boxValid || submitting"
          @click="submit"
        >
          {{ submitting ? "提交中…" : boxValid ? "确认位置，继续处理" : "请先在画面上框出水印" }}
        </button>
        <button class="link-btn" type="button" :disabled="cancelling" @click="cancel">
          {{ cancelling ? "取消中…" : "不处理了，取消并退积分" }}
        </button>
        <span v-if="task.task_no" class="micro muted num task-no">任务号 {{ task.task_no }}</span>
      </div>
    </template>
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

.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: var(--sp-4);
  flex-wrap: wrap;
}

.tips {
  margin-top: var(--sp-4);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--line-soft);
  line-height: 1.9;
}

.actions {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  flex-wrap: wrap;
  margin-top: var(--sp-5);
}

.task-no {
  margin-left: auto;
}

.skeleton {
  animation: pulse 1.4s ease-in-out infinite;
}

.sk-line {
  height: 12px;
  border-radius: 4px;
  background: var(--line-soft);
}

.sk-block {
  height: 320px;
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

  .actions {
    flex-direction: column;
    align-items: stretch;
  }

  .task-no {
    margin-left: 0;
  }
}
</style>
