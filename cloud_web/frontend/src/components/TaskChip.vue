<script setup lang="ts">
import { computed } from "vue";
import { useRouter } from "vue-router";
import TaskStatusPill from "@/components/TaskStatusPill.vue";
import type { TaskListItem } from "@/api/client";

const props = defineProps<{ task: TaskListItem }>();
const router = useRouter();

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
  padding: 16px;
  cursor: pointer;
  font-family: inherit;
  transition: border-color 0.16s, box-shadow 0.16s, transform 0.16s;
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
}

.chip-arrow {
  color: var(--ink-300);
  font-size: 20px;
  line-height: 1;
}

.chip-title {
  font-size: var(--fs-body);
  font-weight: 600;
  color: var(--ink-900);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chip-foot {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

.chip-meta { margin-top: 4px; }
</style>
