<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";
import TaskStatusPill from "@/components/TaskStatusPill.vue";
import type { TaskListItem } from "@/api/client";

const props = defineProps<{ task: TaskListItem }>();
const router = useRouter();
const imageFailed = ref(false);
watch(() => [props.task.thumbnail_url, props.task.preview_url], () => { imageFailed.value = false; });

const title = computed(() => {
  const d = props.task.duration_seconds;
  return d ? `${d.toFixed(1)} 秒视频` : "视频任务";
});

const meta = computed(() => {
  const w = props.task.width;
  const h = props.task.height;
  if (!w || !h) return "";
  return `${h > w ? "竖屏" : "横屏"} · ${w}×${h}`;
});

const summary = computed(() => {
  switch (props.task.status) {
    case "succeeded": return "处理完成，查看结果";
    case "failed": return "处理未完成，查看详情";
    case "cancelled": return "任务已取消";
    case "expired": return "素材已过期";
    case "awaiting_confirmation":
    case "needs_manual": return "等待确认水印位置";
    case "pending":
    case "leased": return props.task.bbox_confirmed ? "等待继续去水印" : "等待算力节点处理";
    default: return "正在处理视频";
  }
});

function open() {
  router.push({ name: "result", params: { id: props.task.id } });
}
</script>

<template>
  <button class="chip" type="button" @click="open">
    <div class="chip-preview">
      <img v-if="task.thumbnail_url && !imageFailed" :src="task.thumbnail_url" :alt="`${title}的首帧画面`" loading="lazy" @error="imageFailed = true" />
      <video v-else-if="task.preview_url && !imageFailed" :src="`${task.preview_url}#t=0.001`" muted playsinline preload="metadata" aria-label="视频首帧预览" @error="imageFailed = true" />
      <div v-else class="preview-empty" aria-hidden="true"><span>▸</span><small>视频预览</small></div>
    </div>
    <div class="chip-top">
      <TaskStatusPill :status="task.status" :progress="task.progress" :confirmed="task.bbox_confirmed" />
      <span class="chip-arrow" aria-hidden="true">↗</span>
    </div>
    <div class="chip-title">{{ title }}</div>
    <div v-if="meta" class="chip-meta micro muted num">{{ meta }}</div>
    <div v-if="task.status === 'running' || task.status === 'removing_watermark'" class="bar" style="margin-top: 9px">
      <i :style="{ width: `${Math.max(3, task.progress * 100)}%` }" />
    </div>
    <div class="chip-foot micro muted">{{ summary }}</div>
  </button>
</template>

<style scoped>
.chip {
  display: block;
  width: 100%;
  text-align: left;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--r-ctl);
  padding: 0;
  cursor: pointer;
  font-family: inherit;
  transition: border-color 0.16s, box-shadow 0.16s, transform 0.16s;
  overflow: hidden;
}

.chip:hover {
  border-color: var(--line-strong);
  box-shadow: var(--shadow-card);
  transform: translateY(-2px);
}

.chip-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 15px;
  padding: 16px 16px 0;
}

.chip-preview { aspect-ratio: 16 / 9; background: var(--surface-sunken); overflow: hidden; }
.chip-preview img { display: block; width: 100%; height: 100%; object-fit: cover; }
.chip-preview video { display: block; width: 100%; height: 100%; object-fit: cover; pointer-events: none; }
.preview-empty { width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; color: var(--ink-300); }
.preview-empty span { font-size: 26px; line-height: 1; }
.preview-empty small { font-size: var(--fs-micro); }

.chip-arrow {
  color: var(--ink-300);
  font-size: 20px;
  line-height: 1;
}

.chip-title {
  padding: 0 16px;
  font-size: var(--fs-body);
  font-weight: 600;
  color: var(--ink-900);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chip-foot {
  margin: 16px 16px 0;
  padding: 12px 0 16px;
  border-top: 1px solid var(--line);
}

.chip-meta { margin-top: 4px; padding: 0 16px; }
</style>
